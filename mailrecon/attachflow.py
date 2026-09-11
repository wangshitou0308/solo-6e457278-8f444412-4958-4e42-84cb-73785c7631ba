"""附件流转追踪引擎（离线、只标注、不猜测）。

输入扁平化后的邮件列表（节点含解析期附件元数据与会话父子关系），沿会话
父子边比较**附件多重集合**，标出四类流转事件：

* ``added`` — 子邮件中新增（父邮件多重集合中没有可匹配的实例）；
* ``removed`` — 相对父邮件被移除（子邮件中不再出现）；
* ``renamed`` — 内容相同（SHA-256 相同）但文件名改变；
* ``content_changed`` — 文件名相同但内容变化（SHA-256 不同）。

多重集合语义：**同一邮件中的重名/重复附件分别计数**——先按
``(文件名, SHA-256)`` 精确抵消，再按 SHA-256 配对改名、按文件名配对
内容变化，剩余实例才计为新增/移除；每个事件带 ``count`` 实例数。
文件名一律用解析期保留的**原始文件名**（``original_filename``），
展示层去重后缀（如 ``report(1).pdf``）与 MIME 顺序变化不影响匹配。

按 SHA-256 归并内容相同的附件，输出内容台账（``attachments``）：每个
唯一内容记录首次出现位置（``first_seen``）与全部来源（``occurrences``，
含每次出现所在邮件、来源文件与文件名）。

**不推断原则**：声称回复但父节点缺失、引用冲突（Message-ID 对应多封
内容不同的邮件、引用边被断开）、附件内容无法解码时，不做流转推断，
只列为 ``reviews`` 待复核；无法解码的附件不参与多重集合比较（其哈希
无意义）。精确匹配/改名/内容变化均在可验证附件之间确认，始终输出；
但一侧存在无法解码附件时，对侧的剩余实例可能是该附件的真实内容，
对应的移除/新增**不生成事件**，只记入邮件行的 ``suppressed`` 待人工复核。
"""

from __future__ import annotations

from typing import Any

# 流转事件类型（用于 events 接口的 type 筛选校验）
EVENT_TYPES = ("added", "removed", "renamed", "content_changed")

# 待复核类型
REVIEW_KINDS = (
    "missing_parent",
    "reference_conflict",
    "undecodable_attachment",
)

# 事件类型中文标签（用于人类可读 summary）
_EVENT_LABELS = {
    "added": "新增",
    "removed": "移除",
    "renamed": "改名",
    "content_changed": "内容变化",
}


def analyze(emails: list[dict[str, Any]]) -> dict[str, Any]:
    """对扁平邮件列表做附件流转追踪。

    ``emails`` 每项需含：``uid``、``message_id``、``subject``、``from``、
    ``date``、``raw_sha256``、``in_reply_to``、``references``、
    ``attachments``（``filename``/``size``/``sha256``，可带
    ``original_filename`` 与 ``undecodable`` 标记）、``issues``、
    ``parent_uid``、``thread_root_uid``、``thread_root_message_id``、
    ``source``。

    返回 ``{"events": [...], "reviews": [...], "attachments": [...],
    "emails": [...], "stats": {...}}``。
    """
    by_uid = {mail["uid"]: mail for mail in emails}

    # ---- Message-ID 分组：识别重复副本与内容冲突 -----------------------
    mid_groups: dict[str, list[dict[str, Any]]] = {}
    for mail in emails:
        mid = mail.get("message_id")
        if mid:
            mid_groups.setdefault(mid, []).append(mail)
    # 同一 Message-ID 对应多封内容不同的邮件 -> 引用冲突（不猜测）
    conflicted_mids = {
        mid
        for mid, group in mid_groups.items()
        if len({m.get("raw_sha256") for m in group}) > 1
    }
    # 规范节点 = 同 ID 中 uid 最小者（与 threads 的“首次出现”口径一致）
    canonical_uid = {
        mid: min(m["uid"] for m in group) for mid, group in mid_groups.items()
    }
    present_mids = set(mid_groups)

    events: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    email_rows: list[dict[str, Any]] = []
    registry: dict[str, dict[str, Any]] = {}

    def add_review(review: dict[str, Any], row_ids: list[str]) -> None:
        review["id"] = f"R{len(reviews) + 1:04d}"
        reviews.append(review)
        row_ids.append(review["id"])

    for mail in sorted(emails, key=lambda m: m["uid"]):
        uid = mail["uid"]
        mid = mail.get("message_id")
        row_event_ids: list[str] = []
        row_review_ids: list[str] = []

        # ---- 附件可验证性拆分（无法解码的不参与比较，只列待复核） -------
        verifiable, undecodable, legacy = _split_attachments(mail)
        for att in undecodable:
            add_review(_undecodable_review(mail, att, legacy),
                       row_review_ids)

        # ---- 内容台账：全部可验证实例按 SHA-256 归并 -------------------
        for index, att in enumerate(verifiable):
            name = _match_name(att)
            entry = registry.setdefault(
                att["sha256"],
                {"sha256": att["sha256"], "size": att["size"],
                 "names": [], "occurrences": []},
            )
            if name not in entry["names"]:
                entry["names"].append(name)
            entry["occurrences"].append(
                {
                    "email": _mail_ref(mail),
                    "filename": name,
                    "attachment_index": index,
                }
            )

        # ---- 父子边流转比较（满足不推断条件时跳过并列待复核） ----------
        compared = False
        skip_reason: str | None = None
        parent_uid = mail.get("parent_uid")
        parent = by_uid.get(parent_uid) if parent_uid is not None else None
        is_noncanonical_dup = (
            mid is not None and canonical_uid.get(mid) != uid
        )

        if is_noncanonical_dup:
            if mid in conflicted_mids:
                # 同一 Message-ID 被内容不同的邮件复用：位置无法确定
                add_review(
                    _conflict_review(mail, mid, mid_groups[mid],
                                     own_dup=True),
                    row_review_ids,
                )
                skip_reason = "reference_conflict"
            else:
                # 字节相同的重复副本：内容经台账归并，不重复参与流转
                skip_reason = "duplicate_copy"
        elif parent is None:
            if _claimed_refs(mail):
                # 声称回复但没有可用父节点
                direct = _direct_ref(mail)
                if direct in present_mids:
                    # 目标在包内但引用边被断开（成环/自引用/重复未挂载）
                    add_review(_edge_conflict_review(mail, direct),
                               row_review_ids)
                else:
                    add_review(_missing_parent_review(mail, direct),
                               row_review_ids)
                skip_reason = reviews[-1]["kind"]
            else:
                skip_reason = "thread_root"
        else:
            direct = _direct_ref(mail)
            if direct is not None and parent.get("message_id") != direct:
                # 直接父缺失/引用边未使用：挂载的是上溯祖先，不做推断
                if direct in present_mids:
                    add_review(_edge_conflict_review(mail, direct),
                               row_review_ids)
                else:
                    add_review(_missing_parent_review(mail, direct),
                               row_review_ids)
                skip_reason = reviews[-1]["kind"]
            elif direct is not None and direct in conflicted_mids:
                # 引用的 Message-ID 对应多封内容不同的邮件：父节点不确定
                add_review(
                    _conflict_review(mail, direct, mid_groups[direct],
                                     own_dup=False),
                    row_review_ids,
                )
                skip_reason = "reference_conflict"
            else:
                compared = True

        suppressed: list[dict[str, Any]] = []
        if compared:
            parent_verifiable, parent_undecodable, _ = _split_attachments(
                parent
            )
            for event, note in _diff_edge(
                mail, parent, parent_verifiable, verifiable,
                parent_has_undecodable=bool(parent_undecodable),
                child_has_undecodable=bool(undecodable),
            ):
                if note is not None:
                    suppressed.append(note)
                    continue
                event["id"] = f"E{len(events) + 1:04d}"
                events.append(event)
                row_event_ids.append(event["id"])

        email_rows.append(
            {
                "uid": uid,
                "message_id": mid,
                "subject": mail.get("subject"),
                "from": mail.get("from"),
                "date": mail.get("date"),
                "source": mail["source"],
                "parent_uid": parent_uid,
                "thread_root_uid": mail.get("thread_root_uid"),
                "attachment_count": len(mail.get("attachments") or []),
                "verifiable_count": len(verifiable),
                "undecodable_count": len(undecodable),
                "compared": compared,
                "skip_reason": skip_reason,
                "event_ids": row_event_ids,
                "review_ids": row_review_ids,
            }
        )
        if suppressed:
            email_rows[-1]["suppressed"] = suppressed

    attachments = _registry_view(registry)
    stats = _build_stats(emails, email_rows, events, reviews, attachments)
    return {
        "events": events,
        "reviews": reviews,
        "attachments": attachments,
        "emails": email_rows,
        "stats": stats,
    }


# ================================================================ 附件拆分

def _match_name(att: dict[str, Any]) -> str:
    """用于多重集合匹配的文件名。

    解析期对同邮件内的重名附件会生成展示层去重名（如 ``report(1).pdf``），
    并把真实文件名记入 ``original_filename``；匹配一律用真实文件名，
    否则展示层后缀或 MIME 顺序变化会被误判为改名。旧版结果文件没有
    ``original_filename`` 字段时回退为展示名。
    """
    return att.get("original_filename") or att["filename"]


def _split_attachments(
    mail: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """把邮件附件拆成（可验证, 无法解码, 是否旧版元数据回退）。

    解析期已打 ``undecodable`` 标记的按标记拆分；旧版结果文件没有该
    标记时，若节点问题中出现附件内容无法解码的记录，则保守地把该邮件
    **全部**附件视为不可验证（无法确定具体哪一个，不做猜测）。
    """
    attachments = mail.get("attachments") or []
    flagged = [att for att in attachments if att.get("undecodable")]
    if flagged:
        verifiable = [att for att in attachments if not att.get("undecodable")]
        return verifiable, flagged, False
    if any(_is_undecodable_issue(i) for i in mail.get("issues") or []):
        # 旧版元数据：只有问题文本，无法定位具体附件 -> 全部不可验证
        legacy = [dict(att, undecodable=True) for att in attachments]
        return [], legacy, True
    return list(attachments), [], False


def _is_undecodable_issue(issue: Any) -> bool:
    """节点问题中关于附件**内容**无法解码的记录（不含文件名解码失败）。"""
    if not isinstance(issue, str) or not issue.startswith("附件"):
        return False
    return (
        "内容无法解码" in issue
        or "传输解码失败" in issue
        or "无法解码出可靠内容" in issue
    )


# ================================================================ 引用检查

def _claimed_refs(mail: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    if mail.get("in_reply_to"):
        refs.append(mail["in_reply_to"])
    refs.extend(mail.get("references") or [])
    return refs


def _direct_ref(mail: dict[str, Any]) -> str | None:
    """声称的直接父邮件标识：In-Reply-To 优先，否则 References 最近一条。"""
    if mail.get("in_reply_to"):
        return mail["in_reply_to"]
    references = mail.get("references") or []
    return references[-1] if references else None


# ================================================================ 多重集合比较

def _remaining_of(
    items: list[dict[str, Any]],
) -> dict[tuple[str, str], list[Any]]:
    """(匹配文件名, SHA-256) -> [剩余实例数, size]。"""
    remaining: dict[tuple[str, str], list[Any]] = {}
    for item in items:
        key = (_match_name(item), item["sha256"])
        cell = remaining.setdefault(key, [0, item["size"]])
        cell[0] += 1
    return remaining


def _group_remaining(
    remaining: dict[tuple[str, str], list[Any]], dimension: str
) -> dict[str, list[list[Any]]]:
    """把剩余实例按维度分组。

    ``dimension="sha256"``：``{sha: [[filename, size, count], ...]}``；
    ``dimension="filename"``：``{filename: [[sha, size, count], ...]}``。
    组内按另一维度排序，保证配对确定性。
    """
    groups: dict[str, list[list[Any]]] = {}
    for (name, sha), (count, size) in sorted(remaining.items()):
        if count <= 0:
            continue
        if dimension == "sha256":
            groups.setdefault(sha, []).append([name, size, count])
        else:
            groups.setdefault(name, []).append([sha, size, count])
    for group in groups.values():
        group.sort(key=lambda cell: cell[0])
    return groups


def _diff_edge(
    mail: dict[str, Any],
    parent: dict[str, Any],
    parent_items: list[dict[str, Any]],
    child_items: list[dict[str, Any]],
    parent_has_undecodable: bool = False,
    child_has_undecodable: bool = False,
) -> list[tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """比较一条父子边的附件多重集合（确定性配对顺序）。

    配对顺序：``(文件名, SHA-256)`` 精确抵消 -> 同 SHA-256 不同名配对为
    改名 -> 同名不同 SHA-256 配对为内容变化 -> 剩余为移除/新增。

    返回 ``[(事件, None) | (None, 抑制说明)]`` 的列表：一侧存在无法解码
    附件时，对侧剩余实例的新增/移除**不生成事件**（该实例可能就是无法
    解码附件的真实内容），改以抑制说明返回，由调用方记入邮件行的
    ``suppressed`` 待人工复核。
    """
    p_rem = _remaining_of(parent_items)
    c_rem = _remaining_of(child_items)

    # 1. 精确抵消（同名同内容，按实例数抵消）
    matched_unchanged = 0
    for key in sorted(set(p_rem) & set(c_rem)):
        common = min(p_rem[key][0], c_rem[key][0])
        p_rem[key][0] -= common
        c_rem[key][0] -= common
        matched_unchanged += common

    evidence_base = {
        "parent_attachment_count": len(parent_items),
        "child_attachment_count": len(child_items),
        "matched_unchanged": matched_unchanged,
    }

    out: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []

    # 2. 改名：SHA-256 相同、文件名不同的剩余实例贪心两两配对
    p_by_sha = _group_remaining(p_rem, "sha256")
    c_by_sha = _group_remaining(c_rem, "sha256")
    for sha in sorted(set(p_by_sha) & set(c_by_sha)):
        for p_cell, c_cell, count in _pair_cells(p_by_sha[sha],
                                                 c_by_sha[sha]):
            p_rem[(p_cell[0], sha)][0] -= count
            c_rem[(c_cell[0], sha)][0] -= count
            out.append(
                (
                    _event(mail, parent, "renamed", evidence_base,
                           filename=c_cell[0], previous_filename=p_cell[0],
                           sha256=sha, size=c_cell[1], count=count),
                    None,
                )
            )

    # 3. 内容变化：文件名相同、SHA-256 不同的剩余实例贪心两两配对
    p_by_name = _group_remaining(p_rem, "filename")
    c_by_name = _group_remaining(c_rem, "filename")
    for name in sorted(set(p_by_name) & set(c_by_name)):
        for p_cell, c_cell, count in _pair_cells(p_by_name[name],
                                                 c_by_name[name]):
            p_rem[(name, p_cell[0])][0] -= count
            c_rem[(name, c_cell[0])][0] -= count
            out.append(
                (
                    _event(mail, parent, "content_changed", evidence_base,
                           filename=name, sha256=c_cell[0],
                           previous_sha256=p_cell[0], size=c_cell[1],
                           previous_size=p_cell[1], count=count),
                    None,
                )
            )

    # 4. 剩余：父邮件侧为移除，子邮件侧为新增；对侧有无法解码附件时抑制
    for (name, sha), (count, size) in sorted(p_rem.items()):
        if count <= 0:
            continue
        if child_has_undecodable:
            out.append(
                (None, _suppressed_note("removed", name, sha, size, count))
            )
        else:
            out.append(
                (
                    _event(mail, parent, "removed", evidence_base,
                           filename=name, sha256=sha, size=size, count=count),
                    None,
                )
            )
    for (name, sha), (count, size) in sorted(c_rem.items()):
        if count <= 0:
            continue
        if parent_has_undecodable:
            out.append(
                (None, _suppressed_note("added", name, sha, size, count))
            )
        else:
            out.append(
                (
                    _event(mail, parent, "added", evidence_base,
                           filename=name, sha256=sha, size=size, count=count),
                    None,
                )
            )
    return out


def _suppressed_note(
    note_type: str, filename: str, sha256: str, size: int, count: int
) -> dict[str, Any]:
    """被抑制的剩余实例说明（记入邮件行的 ``suppressed``）。"""
    side = "子邮件" if note_type == "removed" else "父邮件"
    return {
        "type": note_type,
        "filename": filename,
        "sha256": sha256,
        "size": size,
        "count": count,
        "reason": (
            f"{side}存在无法解码的附件，该实例可能就是其真实内容，"
            f"未生成{_EVENT_LABELS[note_type]}事件，待人工复核"
        ),
    }


def _pair_cells(
    parent_cells: list[list[Any]], child_cells: list[list[Any]]
) -> list[tuple[list[Any], list[Any], int]]:
    """同组实例按排序位置贪心两两配对（双指针，允许多对多）。

    单元为 ``[另一维度值, size, 剩余数]``；返回
    ``[(父单元, 子单元, 配对数)]``，并就地扣减两侧剩余数。
    """
    pairs: list[tuple[list[Any], list[Any], int]] = []
    i = j = 0
    while i < len(parent_cells) and j < len(child_cells):
        p_cell = parent_cells[i]
        c_cell = child_cells[j]
        count = min(p_cell[2], c_cell[2])
        if count > 0:
            pairs.append((p_cell, c_cell, count))
            p_cell[2] -= count
            c_cell[2] -= count
        if p_cell[2] == 0:
            i += 1
        if c_cell[2] == 0:
            j += 1
    return pairs


# ================================================================ 事件与待复核

def _event(
    mail: dict[str, Any],
    parent: dict[str, Any],
    event_type: str,
    evidence_base: dict[str, Any],
    **fields: Any,
) -> dict[str, Any]:
    label = _EVENT_LABELS[event_type]
    count = fields["count"]
    filename = fields["filename"]
    if event_type == "renamed":
        what = f"附件 {fields['previous_filename']!r} 改名为 {filename!r}"
    elif event_type == "content_changed":
        what = f"附件 {filename!r} 内容变化"
    else:
        what = f"附件 {filename!r}{label}"
    event: dict[str, Any] = {
        "id": "",  # 占位，由调用方按生成顺序编号
        "type": event_type,
        "thread_root_uid": mail.get("thread_root_uid"),
        "thread_root_message_id": mail.get("thread_root_message_id"),
        "email": _mail_ref(mail),
        "parent_email": {
            "uid": parent["uid"],
            "message_id": parent.get("message_id"),
            "date": parent.get("date"),
        },
        "time": mail.get("date"),
        "filename": filename,
        "sha256": fields["sha256"],
        "size": fields["size"],
        "count": count,
        "summary": (
            f"{what}（{count} 个实例）：相对父邮件 "
            f"{parent.get('message_id') or '(无 Message-ID)'} 的附件多重集合"
        ),
        "basis": (
            "沿会话父子边比较附件多重集合（先按 (文件名, SHA-256) 精确抵消，"
            "再按 SHA-256 配对改名、按文件名配对内容变化，剩余计为新增/移除）"
        ),
        "evidence": dict(evidence_base),
    }
    if "previous_filename" in fields:
        event["previous_filename"] = fields["previous_filename"]
    if "previous_sha256" in fields:
        event["previous_sha256"] = fields["previous_sha256"]
    if "previous_size" in fields:
        event["previous_size"] = fields["previous_size"]
    return event


def _mail_ref(mail: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": mail["uid"],
        "message_id": mail.get("message_id"),
        "subject": mail.get("subject"),
        "date": mail.get("date"),
        "source": mail["source"],
    }


def _review(
    mail: dict[str, Any], kind: str, summary: str, evidence: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": "",  # 占位，由调用方按生成顺序编号
        "kind": kind,
        "thread_root_uid": mail.get("thread_root_uid"),
        "thread_root_message_id": mail.get("thread_root_message_id"),
        "email": _mail_ref(mail),
        "summary": summary,
        "evidence": evidence,
    }


def _undecodable_review(
    mail: dict[str, Any], att: dict[str, Any], legacy: bool
) -> dict[str, Any]:
    note = (
        "旧版结果元数据缺少逐附件标记，该邮件全部附件按不可验证处理"
        if legacy
        else "解析期标记为内容无法解码（记录的 SHA-256/大小为占位值）"
    )
    name = _match_name(att)
    return _review(
        mail,
        "undecodable_attachment",
        f"附件 {name!r} 内容无法解码，不参与流转推断，仅列为待复核",
        {
            "filename": name,
            "size": att["size"],
            "sha256": att["sha256"],
            "note": note,
        },
    )


def _missing_parent_review(
    mail: dict[str, Any], direct: str | None
) -> dict[str, Any]:
    return _review(
        mail,
        "missing_parent",
        f"邮件声称回复 {direct or '(未知父标识)'}，但该父邮件不在目标"
        "范围内，不做附件流转推断，仅列为待复核",
        {
            "claimed_parent": direct,
            "in_reply_to": mail.get("in_reply_to"),
            "references": list(mail.get("references") or []),
        },
    )


def _edge_conflict_review(
    mail: dict[str, Any], direct: str | None
) -> dict[str, Any]:
    return _review(
        mail,
        "reference_conflict",
        f"邮件声称的直接父邮件 {direct or '(未知父标识)'} 的引用边被断开"
        "（引用成环/自引用或重复邮件未参与挂载），不做附件流转推断，"
        "仅列为待复核",
        {
            "claimed_parent": direct,
            "in_reply_to": mail.get("in_reply_to"),
            "references": list(mail.get("references") or []),
        },
    )


def _conflict_review(
    mail: dict[str, Any],
    conflicted_mid: str,
    group: list[dict[str, Any]],
    own_dup: bool,
) -> dict[str, Any]:
    """Message-ID 冲突待复核项。

    ``conflicted_mid`` 为冲突的 Message-ID（``own_dup=True`` 时是本邮件
    自身的 ID，否则是本邮件声称回复的父邮件 ID）；``group`` 为共享该
    ID 的全部邮件。
    """
    variants = [
        {
            "uid": m["uid"],
            "raw_sha256": m.get("raw_sha256"),
            "source": m["source"],
        }
        for m in sorted(group, key=lambda m: m["uid"])
    ]
    if own_dup:
        summary = (
            f"邮件的 Message-ID {conflicted_mid} 被 {len(group)} 封内容"
            "不同的邮件复用，本邮件不是规范节点，会话位置无法确定，"
            "不做附件流转推断，仅列为待复核"
        )
    else:
        summary = (
            f"邮件声称回复的 Message-ID {conflicted_mid} 对应 "
            f"{len(group)} 封内容不同的邮件，父节点不确定，"
            "不做附件流转推断，仅列为待复核"
        )
    return _review(
        mail,
        "reference_conflict",
        summary,
        {
            "message_id": conflicted_mid,
            "variant_count": len(variants),
            "variants": variants,
        },
    )


# ================================================================ 台账与统计

def _registry_view(registry: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """内容台账：每个唯一内容（SHA-256）的首次出现位置与全部来源。"""
    entries: list[dict[str, Any]] = []
    for entry in registry.values():
        occurrences = entry["occurrences"]
        # 首次出现：按 (无日期排后, 日期, uid, 附件序号) 最早的实例
        first = min(
            occurrences,
            key=lambda occ: (
                occ["email"]["date"] is None,
                occ["email"]["date"] or "",
                occ["email"]["uid"],
                occ["attachment_index"],
            ),
        )
        entries.append(
            {
                "sha256": entry["sha256"],
                "size": entry["size"],
                "names": sorted(entry["names"]),
                "first_seen": {
                    "email": first["email"],
                    "filename": first["filename"],
                },
                "occurrence_count": len(occurrences),
                "occurrences": occurrences,
            }
        )
    entries.sort(
        key=lambda e: (
            e["first_seen"]["email"]["date"] is None,
            e["first_seen"]["email"]["date"] or "",
            e["first_seen"]["email"]["uid"],
            e["sha256"],
        )
    )
    return entries


def _build_stats(
    emails: list[dict[str, Any]],
    email_rows: list[dict[str, Any]],
    events: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    events_by_type = {event_type: 0 for event_type in EVENT_TYPES}
    instances_by_type = {event_type: 0 for event_type in EVENT_TYPES}
    for event in events:
        events_by_type[event["type"]] += 1
        instances_by_type[event["type"]] += event["count"]
    reviews_by_kind = {kind: 0 for kind in REVIEW_KINDS}
    for review in reviews:
        reviews_by_kind[review["kind"]] += 1
    return {
        "emails": len(emails),
        "emails_with_attachments": sum(
            1 for row in email_rows if row["attachment_count"] > 0
        ),
        "attachments_total": sum(
            row["attachment_count"] for row in email_rows
        ),
        "attachments_verifiable": sum(
            row["verifiable_count"] for row in email_rows
        ),
        "attachments_undecodable": sum(
            row["undecodable_count"] for row in email_rows
        ),
        "unique_contents": len(attachments),
        "edges_compared": sum(1 for row in email_rows if row["compared"]),
        "events_total": len(events),
        "event_instances_total": sum(e["count"] for e in events),
        "events_by_type": events_by_type,
        "event_instances_by_type": instances_by_type,
        "reviews_total": len(reviews),
        "reviews_by_kind": reviews_by_kind,
    }
