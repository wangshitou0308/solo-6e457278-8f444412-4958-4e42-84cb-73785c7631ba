"""收件人流转分析引擎（离线、只比较可见头、不猜测送达）。

输入扁平化后的邮件列表（节点含解析期 From/To/Cc 地址、``identity`` 头块
与会话父子关系），沿会话父子边逐条比较**可见地址集合**，记录四类事件：

* ``added`` — 相对父邮件新增的可见地址（子邮件参与者中父参与者没有的）；
* ``dropped`` — 父邮件原 To/Cc 收件人未继续列入子邮件 To/Cc，且不是
  子邮件发件人（即真的从收件人名单消失，而不只是“父发件人变成我”）；
* ``role_changed`` — 同一地址在父子两封邮件间 To/Cc 角色变化
  （To→Cc 或 Cc→To）；
* ``reply_all_omitted`` — 相对父邮件全部参与者（From∪To∪Cc），
  子邮件可见集合（From∪To∪Cc）遗漏的地址（疑似未全部回复）。

匹配规则（离线、可复核）：

* **忽略显示名**：只用 ``local-part@domain`` 比较；
* **域名转小写，local-part 保持原样**：规范键为
  ``local-part + "@" + domain.lower()``；因此 ``A@x.com`` 与 ``a@x.com``
  视为同一地址（域名侧不区分大小写），而 ``John@x.com`` 与 ``john@x.com``
  不合并（local-part 大小写不做猜测）；
* 同一地址在 To/Cc 重复出现时按集合去重，但保留全部出现字段。

**只看可见头，绝不推断实际送达**：不读取、不推断 Bcc，不使用
Return-Path/Sender/Received 等 SMTP 信封信息猜测谁真正收到；分析过程
只读结果 JSON，**绝不改写现有会话树**。

**待复核（不生成客观事件）**：

* 邮件列表/群组迹象（identity 头块的多地址 From、与 From 不同的
  Sender/Reply-To、重复 Sender/Reply-To，或地址形态为列表/群组，
  或 From 原文呈 RFC 群组语法）；
* 畸形地址（To/Cc/From 中无法可靠归一化的地址）；
* 父链缺失（声称回复但父邮件不在范围内、挂载到上溯祖先、引用边断开）；
* 引用冲突（Message-ID 被内容不同的邮件复用）。

命中上述背景的父子边，其差异只以 ``reviews`` 待复核形式给出（仍附集合
差异，便于人工判断），不计入 ``events``。
"""

from __future__ import annotations

import re
from typing import Any

# 流转事件类型（用于 events 接口的 type 筛选校验）
EVENT_TYPES = ("added", "dropped", "role_changed", "reply_all_omitted")

# 待复核类型
REVIEW_KINDS = (
    "possible_mailing_list",
    "malformed_address",
    "missing_parent",
    "reference_conflict",
)

# 参与比较的可见头字段
FIELDS = ("From", "To", "Cc")

# 域名标签：字母/数字/连字符，允许 IDN（非 ASCII 字母），至少 1 个字符
_DOMAIN_LABEL_RE = re.compile(r"^(?![-])[^\s@.]{1,63}(?<!-)$")

# RFC 5322 群组语法迹象：Display-Name: addr, addr; （From 原文中出现分号）
_GROUP_SYNTAX_RE = re.compile(r":[^;]*;")

# 列表/群组地址 local-part 的高置信形态（精确分词，避免误伤正常用户）
_LIST_EXACT = {
    "owners", "owner", "managers", "staff", "team", "crew",
    "admins", "admin", "moderators", "subscribe", "unsubscribe",
    "mailman", "majordomo", "listserv", "noreply", "no-reply",
    "donotreply", "do-not-reply", "bounces", "bounce",
}
_LIST_PREFIXES = (
    "list-", "mailman-", "majordomo-", "no-reply-", "noreply-",
    "bounce-", "bounces-",
)
_LIST_SUFFIXES = (
    "-owners", "-owner", "-managers", "-staff", "-team",
    "-subscribe", "-unsubscribe", "-request", "-bounces", "-bounce",
)


def analyze(emails: list[dict[str, Any]]) -> dict[str, Any]:
    """对扁平邮件列表做收件人流转分析。

    ``emails`` 每项需含：``uid``、``message_id``、``subject``、``from``、
    ``to``、``cc``、``date``、``raw_sha256``、``in_reply_to``、
    ``references``、``issues``、``identity``（可选，用于列表迹象）、
    ``parent_uid``、``thread_root_uid``、``thread_root_message_id``、
    ``source``。

    返回 ``{"events": [...], "reviews": [...], "threads": [...],
    "emails": [...], "addresses": [...], "stats": {...}}``。
    """
    by_uid = {mail["uid"]: mail for mail in emails}

    # ---- 预解析：每封邮件只解析一次，避免父邮件的待复核被多个子邮件重复 ----
    parsed_by_uid = {mail["uid"]: _parse_mail_addresses(mail) for mail in emails}
    hints_by_uid = {
        uid: _mailing_list_hints(by_uid[uid], parsed)
        for uid, parsed in parsed_by_uid.items()
    }

    # ---- Message-ID 分组：识别重复副本与内容冲突 -----------------------
    mid_groups: dict[str, list[dict[str, Any]]] = {}
    for mail in emails:
        mid = mail.get("message_id")
        if mid:
            mid_groups.setdefault(mid, []).append(mail)
    conflicted_mids = {
        mid
        for mid, group in mid_groups.items()
        if len({m.get("raw_sha256") for m in group}) > 1
    }
    canonical_uid = {
        mid: min(m["uid"] for m in group) for mid, group in mid_groups.items()
    }
    present_mids = set(mid_groups)

    events: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    email_rows: list[dict[str, Any]] = []
    edge_rows: list[dict[str, Any]] = []
    registry: dict[str, dict[str, Any]] = {}

    def add_review(review: dict[str, Any], row_ids: list[str]) -> str:
        review["id"] = f"R{len(reviews) + 1:04d}"
        reviews.append(review)
        row_ids.append(review["id"])
        return review["id"]

    for mail in sorted(emails, key=lambda m: m["uid"]):
        uid = mail["uid"]
        mid = mail.get("message_id")
        row_event_ids: list[str] = []
        row_review_ids: list[str] = []

        parsed = parsed_by_uid[uid]
        own_list_hints = hints_by_uid[uid]

        # ---- 地址台账：From/To/Cc 中每个（邮件, 字段）出现都记录一次 ----
        for entry in parsed["from_map"].values():
            _registry_observe(registry, mail, entry, "From")
        for entry in parsed["role_map"]["to"].values():
            _registry_observe(registry, mail, entry, "To")
        for entry in parsed["role_map"]["cc"].values():
            _registry_observe(registry, mail, entry, "Cc")

        # ---- 本邮件自身的畸形地址 / 列表迹象待复核（每邮件只记一次） ----
        for malformed in parsed["malformed"]:
            add_review(_malformed_review(mail, malformed), row_review_ids)
        if own_list_hints:
            add_review(_list_review(mail, own_list_hints), row_review_ids)

        # ---- 父子边判定（与附件流转移口径一致，不猜测） -----------------
        compared = False
        skip_reason: str | None = None
        parent_uid = mail.get("parent_uid")
        parent = by_uid.get(parent_uid) if parent_uid is not None else None
        is_noncanonical_dup = (
            mid is not None and canonical_uid.get(mid) != uid
        )
        edge_kinds: list[str] = []

        if is_noncanonical_dup:
            if mid in conflicted_mids:
                add_review(
                    _conflict_review(
                        mail, mid, mid_groups[mid], parsed_by_uid,
                        own_dup=True, own_parsed=parsed,
                    ),
                    row_review_ids,
                )
                skip_reason = "reference_conflict"
            else:
                skip_reason = "duplicate_copy"
        elif parent is None:
            if _claimed_refs(mail):
                direct = _direct_ref(mail)
                if direct in present_mids:
                    candidate = by_uid[canonical_uid[direct]]
                    add_review(
                        _edge_conflict_review(
                            mail, direct, parsed,
                            candidate, parsed_by_uid[candidate["uid"]],
                        ),
                        row_review_ids,
                    )
                else:
                    add_review(
                        _missing_parent_review(mail, direct, parsed),
                        row_review_ids,
                    )
                skip_reason = "reference_conflict" if direct in present_mids \
                    else "missing_parent"
            else:
                skip_reason = "thread_root"
        else:
            direct = _direct_ref(mail)
            if direct is not None and parent.get("message_id") != direct:
                if direct in present_mids:
                    candidate = by_uid[canonical_uid[direct]]
                    add_review(
                        _edge_conflict_review(
                            mail, direct, parsed,
                            candidate, parsed_by_uid[candidate["uid"]],
                        ),
                        row_review_ids,
                    )
                    skip_reason = "reference_conflict"
                else:
                    add_review(
                        _missing_parent_review(mail, direct, parsed),
                        row_review_ids,
                    )
                    skip_reason = "missing_parent"
            elif direct is not None and direct in conflicted_mids:
                add_review(
                    _conflict_review(
                        mail, direct, mid_groups[direct], parsed_by_uid,
                        own_dup=False, own_parsed=parsed,
                    ),
                    row_review_ids,
                )
                skip_reason = "reference_conflict"
            else:
                compared = True

        # ---- 父子边集合比较 --------------------------------------------
        if parent is not None:
            parent_parsed = parsed_by_uid[parent["uid"]]
            parent_list_hints = hints_by_uid[parent["uid"]]
            diff = _diff_sets(parent_parsed, parsed)

            edge_compared = False
            if compared:
                # 背景信号：本邮件/父邮件有列表迹象或畸形地址 -> 只待复核
                if own_list_hints or parent_list_hints:
                    edge_kinds.append("possible_mailing_list")
                if parsed["malformed"] or parent_parsed["malformed"]:
                    edge_kinds.append("malformed_address")
                if edge_kinds:
                    add_review(
                        _edge_background_review(
                            mail, parent, diff, edge_kinds,
                            own_list_hints, parent_list_hints,
                            parsed["malformed"], parent_parsed["malformed"],
                        ),
                        row_review_ids,
                    )
                else:
                    edge_compared = True
                    for event in _diff_events(mail, parent, diff):
                        event["id"] = f"E{len(events) + 1:04d}"
                        events.append(event)
                        row_event_ids.append(event["id"])

            edge_rows.append(
                _edge_row(
                    mail, parent, diff,
                    compared=edge_compared, edge_kinds=edge_kinds,
                )
            )

        email_rows.append(
            {
                "uid": uid,
                "message_id": mid,
                "subject": mail.get("subject"),
                "from": _first_addr(parsed),
                "date": mail.get("date"),
                "source": mail["source"],
                "parent_uid": parent_uid,
                "thread_root_uid": mail.get("thread_root_uid"),
                "visible_count": len(parsed["participants"]),
                "to_count": len(parsed["role_map"]["to"]),
                "cc_count": len(parsed["role_map"]["cc"]),
                "malformed_count": len(parsed["malformed"]),
                "list_hints": bool(own_list_hints),
                "compared": bool(parent is not None and compared and not edge_kinds),
                "skip_reason": skip_reason,
                "event_ids": row_event_ids,
                "review_ids": row_review_ids,
            }
        )

    addresses = _registry_view(registry)
    threads = _thread_summaries(emails, email_rows)
    stats = _build_stats(emails, email_rows, edge_rows, events, reviews, addresses)
    return {
        "events": events,
        "reviews": reviews,
        "threads": threads,
        "emails": email_rows,
        "addresses": addresses,
        "stats": stats,
    }


# ================================================================ 地址解析

def _parse_mail_addresses(mail: dict[str, Any]) -> dict[str, Any]:
    """抽取一封邮件 From/To/Cc 的可归一化地址与畸形条目。

    返回：

    * ``entries``：``[{"key", "address", "name", "field", "role"}]``，
      同一封邮件内同一规范键只保留首次出现（跨字段也去重，但角色映射
      在 ``role_map`` 中保留全部 To/Cc 归属）；
    * ``role_map``：``{"to": {key: entry}, "cc": {...}}``；
    * ``from_map``：From 规范键 -> entry（正常仅一个）；
    * ``participants``：From∪To∪Cc 规范键集合；
    * ``malformed``：无法可靠归一化的原始地址条目。
    """
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    role_map: dict[str, dict[str, dict[str, Any]]] = {"to": {}, "cc": {}}
    from_map: dict[str, dict[str, Any]] = {}
    malformed: list[dict[str, Any]] = []

    def ingest(field: str, role: str | None, item: Any) -> None:
        raw_name = ""
        raw_addr = ""
        if isinstance(item, dict):
            raw_name = str(item.get("name") or "")
            raw_addr = str(item.get("address") or "")
        elif isinstance(item, str):
            raw_addr = item
        raw_addr = raw_addr.strip()
        key = _normalize(raw_addr)
        if key is None:
            if raw_addr:  # 有值但无法归一化 -> 畸形（空值直接忽略）
                malformed.append(
                    {
                        "field": field,
                        "raw_address": raw_addr,
                        "name": raw_name,
                        "reason": _malformed_reason(raw_addr),
                    }
                )
            return
        if key in seen:
            # 同地址重复出现：仍登记角色（To 与 Cc 都出现等），不重复入列
            entry = next(e for e in entries if e["key"] == key)
            if role == "to":
                role_map["to"][key] = entry
            elif role == "cc":
                role_map["cc"][key] = entry
            return
        entry = {
            "key": key,
            "address": key,  # 归一化后地址（域名小写、local-part 原样）
            "name": raw_name,
        }
        entries.append(entry)
        seen.add(key)
        if role == "to":
            role_map["to"][key] = entry
        elif role == "cc":
            role_map["cc"][key] = entry
        elif field == "From":
            from_map[key] = entry

    from_item = mail.get("from")
    if from_item:
        ingest("From", None, from_item)
    for item in mail.get("to") or []:
        ingest("To", "to", item)
    for item in mail.get("cc") or []:
        ingest("Cc", "cc", item)

    participants = set(from_map) | set(role_map["to"]) | set(role_map["cc"])
    return {
        "entries": entries,
        "role_map": role_map,
        "from_map": from_map,
        "participants": participants,
        "malformed": malformed,
    }


def _normalize(raw_addr: str) -> str | None:
    """把裸地址归一化为比较键：域名小写、local-part 保持原样。

    无法可靠归一化（@ 数量异常、local-part/域名为空、域名标签非法、
    含空白等）时返回 ``None``——调用方按畸形地址处理，绝不猜测。
    """
    if not raw_addr or any(ch.isspace() for ch in raw_addr):
        return None
    if raw_addr.count("@") != 1:
        return None
    local, domain = raw_addr.split("@")
    if not local or not domain or len(raw_addr) > 254:
        return None
    if local.startswith('"'):
        # 引号 local-part 形态复杂，不猜测其比较语义
        return None
    labels = domain.split(".")
    if any(not _DOMAIN_LABEL_RE.match(label) for label in labels):
        return None
    return f"{local}@{domain.lower()}"


def _malformed_reason(raw_addr: str) -> str:
    if not raw_addr:
        return "地址为空"
    if any(ch.isspace() for ch in raw_addr):
        return "地址包含空白字符"
    if raw_addr.count("@") != 1:
        return f"地址中 '@' 数量异常（{raw_addr.count('@')} 个）"
    local, _, domain = raw_addr.partition("@")
    if not local:
        return "local-part 为空"
    if not domain:
        return "域名部分为空"
    if local.startswith('"'):
        return "引号 local-part 不参与自动比较"
    return "域名标签非法"


# ================================================================ 列表迹象

def _mailing_list_hints(
    mail: dict[str, Any], parsed: dict[str, Any]
) -> list[str]:
    """收集邮件列表/群组地址迹象（只使用离线可得的信号）。"""
    hints: list[str] = []

    # 1) identity 头块：多地址 From、与 From 不同的 Sender/Reply-To、
    #    Sender/Reply-To 重复头（与声明身份核验同口径）
    identity = mail.get("identity") or {}
    from_entries = identity.get("from") or []
    sender_entries = identity.get("sender") or []
    reply_entries = identity.get("reply_to") or []

    if len(from_entries) > 1 or any(
        len(e.get("addresses") or []) > 1 for e in from_entries
    ):
        hints.append("From 含多个地址（RFC 6854 群组语法，常见于列表/转发）")
    from_first = from_entries[0] if from_entries else None
    from_addr = (from_first.get("address") or "").lower() if from_first else ""
    sender_first = sender_entries[0] if sender_entries else None
    if (
        sender_first is not None
        and sender_first.get("address")
        and from_addr
        and sender_first["address"].lower() != from_addr
    ):
        hints.append("存在与 From 不同的 Sender 头（列表代发典型特征）")
    reply_first = reply_entries[0] if reply_entries else None
    if (
        reply_first is not None
        and reply_first.get("domain")
        and from_first is not None
        and from_first.get("domain")
        and reply_first["domain"] != from_first["domain"]
    ):
        hints.append("Reply-To 指向与 From 不同的域（列表/代发常见）")
    if len(sender_entries) > 1 or len(reply_entries) > 1:
        hints.append("Sender/Reply-To 出现重复头")

    # 2) From 原文呈群组语法 "组名: a@x, b@y;"
    for entry in from_entries:
        raw = entry.get("raw") or ""
        if _GROUP_SYNTAX_RE.search(raw):
            hints.append(f"From 头呈 RFC 群组语法（含 '…: …;'）: {raw.strip()!r}")
            break

    # 3) 可见地址本身为高置信列表/群组形态（local-part 精确分词）
    listish: list[str] = []
    for addr in parsed["entries"]:
        local = addr["key"].rsplit("@", 1)[0].lower()
        if local in _LIST_EXACT:
            listish.append(addr["key"])
        elif local.startswith(_LIST_PREFIXES) or local.endswith(_LIST_SUFFIXES):
            listish.append(addr["key"])
    if listish:
        hints.append(
            "存在列表/群组形态地址（local-part 为 "
            "owners/request/bounce/subscribe 等）: " + ", ".join(sorted(set(listish)))
        )

    return hints


# ================================================================ 引用检查

def _claimed_refs(mail: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    if mail.get("in_reply_to"):
        refs.append(mail["in_reply_to"])
    refs.extend(mail.get("references") or [])
    return refs


def _direct_ref(mail: dict[str, Any]) -> str | None:
    if mail.get("in_reply_to"):
        return mail["in_reply_to"]
    references = mail.get("references") or []
    return references[-1] if references else None


# ================================================================ 集合差异

def _diff_sets(
    parent: dict[str, Any], child: dict[str, Any]
) -> dict[str, Any]:
    """计算一条父子边的可见地址集合差异。

    角色比较只看 To/Cc（From 不是收件人角色）；参与者集合比较用
    From∪To∪Cc（reply-all 口径）。
    """
    p_to = set(parent["role_map"]["to"])
    p_cc = set(parent["role_map"]["cc"])
    c_to = set(child["role_map"]["to"])
    c_cc = set(child["role_map"]["cc"])
    p_from = set(parent["from_map"])
    c_from = set(child["from_map"])

    p_recip = p_to | p_cc
    c_recip = c_to | c_cc
    p_participants = p_from | p_recip
    c_participants = c_from | c_recip

    added = c_participants - p_participants

    # To/Cc 角色变化（同一地址父子两侧都在 To/Cc，但归属不同）
    role_changes: list[dict[str, Any]] = []
    for key in sorted((p_to | p_cc) & (c_to | c_cc)):
        was = "to" if key in p_to else "cc"
        now = "to" if key in c_to else "cc"
        if was != now:
            role_changes.append(
                {
                    "address": key,
                    "from_role": was,
                    "to_role": now,
                    "parent_fields": sorted(
                        f for f, coll in (("To", p_to), ("Cc", p_cc))
                        if key in coll
                    ),
                    "child_fields": sorted(
                        f for f, coll in (("To", c_to), ("Cc", c_cc))
                        if key in coll
                    ),
                }
            )

    # 未继续列入收件人：父 To/Cc 中有、子 To/Cc 中没有，
    # 且不是子邮件发件人（排除“回复时父发件人成为我”的正常情况）
    dropped = (p_recip - c_recip) - c_from

    # reply-all 遗漏：相对父邮件全部参与者，子可见集合缺失的地址
    omitted = p_participants - c_participants

    diff = {
        "added": sorted(added),
        "dropped": sorted(dropped),
        "role_changed": role_changes,
        "reply_all_omitted": sorted(omitted),
        "parent_from": sorted(p_from),
        "parent_to": sorted(p_to),
        "parent_cc": sorted(p_cc),
        "child_from": sorted(c_from),
        "child_to": sorted(c_to),
        "child_cc": sorted(c_cc),
    }
    return diff


def _diff_events(
    mail: dict[str, Any], parent: dict[str, Any], diff: dict[str, Any]
) -> list[dict[str, Any]]:
    """把集合差异展开为逐地址事件（固定顺序：added→role→dropped→omitted）。"""
    events: list[dict[str, Any]] = []
    base = {
        "parent_participant_count": len(
            set(diff["parent_from"])
            | set(diff["parent_to"])
            | set(diff["parent_cc"])
        ),
        "child_participant_count": len(
            set(diff["child_from"])
            | set(diff["child_to"])
            | set(diff["child_cc"])
        ),
    }

    for address in diff["added"]:
        events.append(
            _event(mail, parent, "added", address, base, diff,
                   fields=_child_fields_for(address, diff),
                   summary=(
                       f"新增可见地址 {address}：父邮件参与者集合中没有该地址"
                   ))
        )
    for change in diff["role_changed"]:
        events.append(
            _event(
                mail, parent, "role_changed", change["address"], base, diff,
                fields=["To", "Cc"],
                summary=(
                    f"地址 {change['address']} 收件角色由 "
                    f"{change['from_role'].upper()} 变为 {change['to_role'].upper()}"
                ),
                extra={
                    "from_role": change["from_role"],
                    "to_role": change["to_role"],
                    "parent_fields": change["parent_fields"],
                    "child_fields": change["child_fields"],
                },
            )
        )
    for address in diff["dropped"]:
        events.append(
            _event(
                mail, parent, "dropped", address, base, diff,
                fields=_parent_fields_for(address, diff),
                summary=(
                    f"地址 {address} 未继续列入子邮件 To/Cc"
                    "（父邮件原收件人，且非子邮件发件人）"
                ),
            )
        )
    for address in diff["reply_all_omitted"]:
        events.append(
            _event(
                mail, parent, "reply_all_omitted", address, base, diff,
                fields=FIELDS,
                summary=(
                    f"地址 {address} 相对父邮件参与者集合被遗漏"
                    "（疑似未全部回复；仅据可见头，不含 Bcc/信封）"
                ),
            )
        )
    return events


def _parent_fields_for(address: str, diff: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    if address in set(diff["parent_from"]):
        fields.append("From")
    if address in set(diff["parent_to"]):
        fields.append("To")
    if address in set(diff["parent_cc"]):
        fields.append("Cc")
    return fields


def _child_fields_for(address: str, diff: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    if address in set(diff["child_from"]):
        fields.append("From")
    if address in set(diff["child_to"]):
        fields.append("To")
    if address in set(diff["child_cc"]):
        fields.append("Cc")
    return fields


# ================================================================ 事件/复核

def _event(
    mail: dict[str, Any],
    parent: dict[str, Any],
    event_type: str,
    address: str,
    base: dict[str, Any],
    diff: dict[str, Any],
    *,
    fields: tuple[str, ...] | list[str],
    summary: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
        "address": address,
        "fields": list(fields),
        "summary": summary,
        "basis": (
            "沿会话父子边比较 From/To/Cc 可见地址集合（忽略显示名，"
            "域名转小写、local-part 保持原样）；不使用 Bcc 或 SMTP 信封，"
            "不推断实际送达对象"
        ),
        "evidence": {
            **base,
            "sets": _set_snapshot(diff),
        },
    }
    if extra:
        event["evidence"].update(extra)
    return event


def _set_snapshot(diff: dict[str, Any]) -> dict[str, Any]:
    return {
        "parent": {
            "from": diff["parent_from"],
            "to": diff["parent_to"],
            "cc": diff["parent_cc"],
        },
        "child": {
            "from": diff["child_from"],
            "to": diff["child_to"],
            "cc": diff["child_cc"],
        },
        "differences": {
            "added": diff["added"],
            "dropped": diff["dropped"],
            "role_changed": diff["role_changed"],
            "reply_all_omitted": diff["reply_all_omitted"],
        },
    }


def _mail_ref(mail: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": mail["uid"],
        "message_id": mail.get("message_id"),
        "subject": mail.get("subject"),
        "date": mail.get("date"),
        "source": mail["source"],
    }


def _review(
    mail: dict[str, Any],
    kind: str,
    summary: str,
    evidence: dict[str, Any],
    *,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    review: dict[str, Any] = {
        "id": "",  # 占位，由调用方按生成顺序编号
        "kind": kind,
        "thread_root_uid": mail.get("thread_root_uid"),
        "thread_root_message_id": mail.get("thread_root_message_id"),
        "email": _mail_ref(mail),
        "fields": fields or list(FIELDS),
        "summary": summary,
        "evidence": evidence,
    }
    return review


def _malformed_review(
    mail: dict[str, Any], malformed: dict[str, Any]
) -> dict[str, Any]:
    return _review(
        mail,
        "malformed_address",
        f"{malformed['field']} 头中地址 {malformed['raw_address']!r} 形态畸形，"
        "该地址不参与自动集合比较，相关差异仅列为待复核",
        {
            "field": malformed["field"],
            "raw_address": malformed["raw_address"],
            "name": malformed.get("name"),
            "reason": malformed["reason"],
        },
        fields=[malformed["field"]],
    )


def _list_review(mail: dict[str, Any], hints: list[str]) -> dict[str, Any]:
    return _review(
        mail,
        "possible_mailing_list",
        "邮件存在邮件列表/群组地址迹象，收件人流转可能经列表改写，"
        "该邮件参与的父子边差异只列为待复核",
        {"hints": hints},
        fields=["From", "Sender", "Reply-To", "To", "Cc"],
    )


def _edge_background_review(
    mail: dict[str, Any],
    parent: dict[str, Any],
    diff: dict[str, Any],
    kinds: list[str],
    own_hints: list[str],
    parent_hints: list[str],
    own_malformed: list[dict[str, Any]],
    parent_malformed: list[dict[str, Any]],
) -> dict[str, Any]:
    kind = "possible_mailing_list" if "possible_mailing_list" in kinds else \
        "malformed_address"
    side: list[str] = []
    if own_hints or own_malformed:
        side.append("子邮件")
    if parent_hints or parent_malformed:
        side.append("父邮件")
    return _review(
        mail,
        kind,
        f"父子边（父 {parent.get('message_id') or '(无 Message-ID)'} → "
        f"子 {mail.get('message_id') or '(无 Message-ID)'}）的"
        f"{'/'.join(side)}存在列表/群组或畸形地址背景，集合差异不升为客观"
        "事件，仅附差异快照待人工复核",
        {
            "parent_email": {
                "uid": parent["uid"],
                "message_id": parent.get("message_id"),
            },
            "background_kinds": kinds,
            "child_list_hints": own_hints,
            "parent_list_hints": parent_hints,
            "child_malformed": own_malformed,
            "parent_malformed": parent_malformed,
            "sets": _set_snapshot(diff),
        },
    )


def _missing_parent_review(
    mail: dict[str, Any],
    direct: str | None,
    parsed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "claimed_parent": direct,
        "in_reply_to": mail.get("in_reply_to"),
        "references": list(mail.get("references") or []),
    }
    if parsed is not None:
        # 子邮件（声称回复方）自身的可见地址集合，便于与日后补齐的父邮件核对
        evidence["child_email"] = _mail_ref(mail)
        evidence["addresses"] = sorted(parsed["participants"])
        evidence["sets"] = {"child": _address_set_view(parsed)}
    return _review(
        mail,
        "missing_parent",
        f"邮件声称回复 {direct or '(未知父标识)'}，但父邮件不在目标范围内"
        "（缺失或挂载到上溯祖先），无法比较收件人流转，仅列为待复核",
        evidence,
        fields=["In-Reply-To", "References"],
    )


def _edge_conflict_review(
    mail: dict[str, Any],
    direct: str | None,
    parsed: dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
    candidate_parsed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """引用边被断开（成环/自引用/重复未挂载）的待复核。

    当声称的直接父 Message-ID 对应邮件可定位（``candidate``）时，附上
    候选父邮件、两封邮件的地址集合与按该候选计算的集合差异，但**不据此
    选择父节点、不生成客观事件**，仅便于人工核对。
    """
    evidence: dict[str, Any] = {
        "claimed_parent": direct,
        "in_reply_to": mail.get("in_reply_to"),
        "references": list(mail.get("references") or []),
    }
    if parsed is not None:
        evidence["addresses"] = sorted(parsed["participants"])
        evidence["sets"] = {"child": _address_set_view(parsed)}
    if candidate is not None and candidate_parsed is not None and parsed is not None:
        provisional = _diff_sets(candidate_parsed, parsed)
        evidence["parent_email"] = _mail_ref(candidate)
        evidence["sets"] = {
            "child": _address_set_view(parsed),
            "parent_candidate": _address_set_view(candidate_parsed),
        }
        evidence["provisional_differences"] = _difference_block(provisional)
        evidence["provisional_note"] = (
            "以下集合差异按声称的直接父邮件（引用边已断开）临时计算，"
            "不代表确定的父子关系，不生成客观事件，仅供人工复核"
        )
    return _review(
        mail,
        "reference_conflict",
        f"邮件声称的直接父邮件 {direct or '(未知父标识)'} 引用边被断开"
        "（引用成环/自引用或重复邮件未参与挂载），无法可靠确定父节点，"
        "收件人流转差异仅列为待复核",
        evidence,
        fields=["Message-ID", "In-Reply-To", "References", "From", "To", "Cc"],
    )


def _address_set_view(parsed: dict[str, Any]) -> dict[str, Any]:
    """一封邮件 From/To/Cc 可见地址集合（用于待复核证据，不含显示名）。"""
    return {
        "from": sorted(parsed["from_map"]),
        "to": sorted(parsed["role_map"]["to"]),
        "cc": sorted(parsed["role_map"]["cc"]),
    }


def _difference_block(diff: dict[str, Any]) -> dict[str, Any]:
    return {
        "added": diff["added"],
        "dropped": diff["dropped"],
        "role_changed": diff["role_changed"],
        "reply_all_omitted": diff["reply_all_omitted"],
    }


def _parent_candidate(
    candidate: dict[str, Any], candidate_parsed: dict[str, Any]
) -> dict[str, Any]:
    """冲突候选父邮件的并列展示单元（含地址集合，供人工核对，不做取舍）。"""
    return {
        "uid": candidate["uid"],
        "message_id": candidate.get("message_id"),
        "date": candidate.get("date"),
        "subject": candidate.get("subject"),
        "source": candidate["source"],
        "raw_sha256": candidate.get("raw_sha256"),
        "addresses": sorted(candidate_parsed["participants"]),
        "sets": _address_set_view(candidate_parsed),
    }


def _conflict_review(
    mail: dict[str, Any],
    conflicted_mid: str,
    group: list[dict[str, Any]],
    parsed_by_uid: dict[int, dict[str, Any]],
    own_dup: bool,
    own_parsed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Message-ID 冲突待复核。

    * ``own_dup=True``：本邮件自身的 ID 被多封内容不同的邮件复用
      （``group`` 即共享该 ID 的全部邮件，本邮件在其中）；
    * ``own_dup=False``：本邮件声称回复的父 ID 对应多封内容不同的邮件
      （``group`` 为候选父邮件）。

    两种形态都并列展示冲突各方的地址集合；声称回复的形态还按每个候选
    父邮件临时计算集合差异（``candidate_differences``），但**不选择父
    节点、不生成客观事件、不改写会话树**。
    """
    variants = [
        _parent_candidate(m, parsed_by_uid[m["uid"]])
        for m in sorted(group, key=lambda m: m["uid"])
    ]
    evidence: dict[str, Any] = {
        "message_id": conflicted_mid,
        "variant_count": len(variants),
        "variants": variants,
    }
    if own_dup:
        summary = (
            f"邮件的 Message-ID {conflicted_mid} 被 {len(group)} 封内容不同"
            "的邮件复用，本邮件不是规范节点，会话位置无法确定，收件人流转"
            "差异仅列为待复核"
        )
        if own_parsed is not None:
            evidence["addresses"] = sorted(own_parsed["participants"])
            evidence["sets"] = {"child": _address_set_view(own_parsed)}
    else:
        summary = (
            f"邮件声称回复的 Message-ID {conflicted_mid} 对应 "
            f"{len(group)} 封内容不同的邮件，父节点不确定，收件人流转差异"
            "仅列为待复核"
        )
        if own_parsed is not None:
            provisional = []
            for candidate in sorted(group, key=lambda m: m["uid"]):
                cand_parsed = parsed_by_uid[candidate["uid"]]
                diff = _diff_sets(cand_parsed, own_parsed)
                provisional.append(
                    {
                        "parent_uid": candidate["uid"],
                        "parent_message_id": candidate.get("message_id"),
                        "addresses": sorted(cand_parsed["participants"]),
                        "differences": _difference_block(diff),
                    }
                )
            evidence["addresses"] = sorted(own_parsed["participants"])
            evidence["sets"] = {"child": _address_set_view(own_parsed)}
            evidence["candidate_differences"] = provisional
            evidence["provisional_note"] = (
                "candidate_differences 分别按每个候选父邮件临时计算，"
                "候选之间不做取舍；不生成客观事件，仅供人工复核"
            )
    review = _review(
        mail,
        "reference_conflict",
        summary,
        evidence,
        fields=["Message-ID", "In-Reply-To", "References", "From", "To", "Cc"],
    )
    # 声称回复的形态可定位候选父邮件（并列、不选择）；自身冲突形态没有
    # 唯一父节点，parent_email 置 null
    if not own_dup and group:
        review["parent_email"] = None
    return review


# ================================================================ 边/台账/会话

def _edge_row(
    mail: dict[str, Any],
    parent: dict[str, Any],
    diff: dict[str, Any],
    *,
    compared: bool,
    edge_kinds: list[str],
) -> dict[str, Any]:
    return {
        "child_uid": mail["uid"],
        "parent_uid": parent["uid"],
        "thread_root_uid": mail.get("thread_root_uid"),
        "compared": compared,
        "review_kinds": edge_kinds,
        "added": diff["added"],
        "dropped": diff["dropped"],
        "role_changed": diff["role_changed"],
        "reply_all_omitted": diff["reply_all_omitted"],
    }


def _first_addr(parsed: dict[str, Any]) -> dict[str, Any] | None:
    """邮件行的 From 便捷字段：归一化地址（无 From 时为 null）。"""
    if parsed["from_map"]:
        entry = next(iter(parsed["from_map"].values()))
        return {"address": entry["address"], "name": entry["name"]}
    return None


def _registry_observe(
    registry: dict[str, dict[str, Any]],
    mail: dict[str, Any],
    entry: dict[str, Any],
    field: str,
) -> None:
    key = entry["key"]
    item = registry.setdefault(
        key,
        {
            "address": key,
            "names": [],
            "fields": set(),
            "occurrences": [],
        },
    )
    if entry["name"] and entry["name"] not in item["names"]:
        item["names"].append(entry["name"])
    item["fields"].add(field)
    item["occurrences"].append(
        {
            "email": _mail_ref(mail),
            "field": field,
        }
    )


def _registry_view(registry: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in registry.values():
        occurrences = item["occurrences"]
        first = min(
            occurrences,
            key=lambda occ: (
                occ["email"]["date"] is None,
                occ["email"]["date"] or "",
                occ["email"]["uid"],
            ),
        )
        entries.append(
            {
                "address": item["address"],
                "names": item["names"],
                "fields": sorted(item["fields"]),
                "first_seen": first,
                "occurrence_count": len(occurrences),
                "occurrences": occurrences,
            }
        )
    entries.sort(
        key=lambda e: (
            e["first_seen"]["email"]["date"] is None,
            e["first_seen"]["email"]["date"] or "",
            e["first_seen"]["email"]["uid"],
            e["address"],
        )
    )
    return entries


def _thread_summaries(
    emails: list[dict[str, Any]],
    email_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按会话根 uid 汇总邮件数、事件与待复核 ID。"""
    roots: dict[Any, dict[str, Any]] = {}
    rows_by_uid = {row["uid"]: row for row in email_rows}
    for mail in emails:
        root_uid = mail.get("thread_root_uid")
        cell = roots.setdefault(
            root_uid,
            {
                "root_uid": root_uid,
                "root_message_id": mail.get("thread_root_message_id"),
                "email_count": 0,
                "node_uids": [],
                "event_ids": [],
                "review_ids": [],
            },
        )
        cell["email_count"] += 1
        cell["node_uids"].append(mail["uid"])
        row = rows_by_uid.get(mail["uid"])
        if row:
            cell["event_ids"].extend(row["event_ids"])
            cell["review_ids"].extend(row["review_ids"])
    out = []
    for root_uid in sorted(roots, key=lambda x: (x is None, x)):
        cell = roots[root_uid]
        cell["node_uids"] = sorted(set(cell["node_uids"]))
        cell["event_ids"] = sorted(set(cell["event_ids"]))
        cell["review_ids"] = sorted(set(cell["review_ids"]))
        cell["event_count"] = len(cell["event_ids"])
        cell["review_count"] = len(cell["review_ids"])
        out.append(cell)
    return out


def _build_stats(
    emails: list[dict[str, Any]],
    email_rows: list[dict[str, Any]],
    edge_rows: list[dict[str, Any]],
    events: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    addresses: list[dict[str, Any]],
) -> dict[str, Any]:
    events_by_type = {event_type: 0 for event_type in EVENT_TYPES}
    for event in events:
        events_by_type[event["type"]] += 1
    reviews_by_kind = {kind: 0 for kind in REVIEW_KINDS}
    for review in reviews:
        reviews_by_kind[review["kind"]] += 1
    return {
        "emails": len(emails),
        "unique_addresses": len(addresses),
        "malformed_addresses": sum(
            row["malformed_count"] for row in email_rows
        ),
        "edges_total": len(edge_rows),
        "edges_compared": sum(1 for row in edge_rows if row["compared"]),
        "edges_with_changes": sum(
            1
            for row in edge_rows
            if row["added"] or row["dropped"]
            or row["role_changed"] or row["reply_all_omitted"]
        ),
        "events_total": len(events),
        "events_by_type": events_by_type,
        "reviews_total": len(reviews),
        "reviews_by_kind": reviews_by_kind,
        "threads_total": len({m.get("thread_root_uid") for m in emails}),
    }
