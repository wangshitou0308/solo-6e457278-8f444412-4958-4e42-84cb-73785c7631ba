"""邮件声明身份核验引擎（离线、只标注、不判定伪造）。

输入扁平化后的邮件列表（节点含解析期 ``identity`` 头块与会话父子关系），
按邮件和会话汇总四类**可观察到的声明差异**：

* ``from_domain_mismatch`` — From 与 Sender / Return-Path 的域不一致；
* ``reply_identity_change`` — 回复链上身份相对父邮件发生变化；
* ``message_id_domain_drift`` — Message-ID 标识域与 From 域不一致
  （邮件级），或同一会话内同一会话参与者的标识域发生漂移（会话级）；
* ``dkim_from_not_covered`` — DKIM-Signature 的 ``h=`` 未覆盖 From。

**明确不做**：不查 DNS、不校验 DKIM 签名（``b=``）真伪、不评价 SPF/DMARC
对齐、不做任何“伪造”定性。转发（``Fwd:``）、邮件列表迹象（多地址 From、
Sender 与 From 不同、Reply-To 指向其他域）、字段缺失等情况**只记为
``needs_review`` 待复核证据**；字段缺失导致无法判断时结论状态为
``inconclusive``（无法核验）。

每条发现（finding）与待复核项（review_flag）都附：

* ``sources`` — 来源文件/作业（作业：``{job_id, source_file}``；
  案件：``{case_id, sources: [...]}``）；
* ``headers`` — 依据的具体头字段（如 ``["From", "Sender"]``）；
* ``evidence`` / ``basis`` — 具体取值与人类可读依据。

发现状态：``observed``（观察到差异）、``needs_review``（存在转发/列表等
待复核背景）、``inconclusive``（证据不足，无法核验）。
"""

from __future__ import annotations

import re
from typing import Any

# 四类发现
FINDING_TYPES = (
    "from_domain_mismatch",
    "reply_identity_change",
    "message_id_domain_drift",
    "dkim_from_not_covered",
)

# 待复核证据类型
REVIEW_KINDS = (
    "possible_forward",
    "possible_mailing_list",
    "missing_header",
    "header_parse_anomaly",
)

# 转发主题前缀（英文 + 中文常见形态）
_FORWARD_PREFIX_RE = re.compile(
    r"^\s*(fwd?|fw|转发|转发：|自动转发)\s*[:：]", re.IGNORECASE
)
# Re: 前缀（用于在转发主题里识别“转发的回复”，仅做前缀剥离）
_RE_PREFIX_RE = re.compile(r"^\s*(re|回复|答复)\s*[:：]\s*", re.IGNORECASE)


def analyze(emails: list[dict[str, Any]]) -> dict[str, Any]:
    """对扁平邮件列表做声明身份核验。

    ``emails`` 每项需含：``uid``、``message_id``、``subject``、
    ``parent_uid``、``source``（来源描述）、``identity``（解析期头块）。

    返回 ``{"findings": [...], "email_reports": [...],
    "thread_reports": [...], "review_flags": [...], "stats": {...}}``。
    """
    findings: list[dict[str, Any]] = []
    review_flags: list[dict[str, Any]] = []
    by_uid = {mail["uid"]: mail for mail in emails}

    # ---- 每封邮件：头级信号与三类邮件级检查 ------------------------------
    email_rows: list[dict[str, Any]] = []
    signals_by_uid: dict[int, list[str]] = {}
    for mail in emails:
        row = _check_one_email(mail, findings, review_flags)
        email_rows.append(row)
        signals_by_uid[mail["uid"]] = row["signals"]

    # ---- 会话级：回复链身份变化 / 标识域漂移 -----------------------------
    thread_rows = _check_threads(
        emails, by_uid, signals_by_uid, findings, review_flags
    )

    # 编号（按生成顺序，稳定可复核）。先原地写 id（对象身份不变，供会话
    # 报告回填），回填后再重排键序让 id 出现在最前
    for seq, f in enumerate(findings, start=1):
        f["id"] = f"F{seq:04d}"
    for seq, flag in enumerate(review_flags, start=1):
        flag["id"] = f"R{seq:04d}"

    # 会话报告回填结论编号
    finding_id_by_obj = {id(f): f["id"] for f in findings}
    for report, fobjs in thread_rows:
        report["finding_ids"] = [
            finding_id_by_obj[id(f)]
            for f in fobjs
            if id(f) in finding_id_by_obj
        ]
        del report["_finding_objs"]

    _reorder_key_first(findings, "id")
    _reorder_key_first(review_flags, "id")

    stats = _build_stats(
        emails, findings, review_flags, email_rows,
        [report for report, _ in thread_rows],
    )
    return {
        "findings": findings,
        "review_flags": review_flags,
        "email_reports": [row["report"] for row in email_rows],
        "thread_reports": [report for report, _ in thread_rows],
        "stats": stats,
    }


# ================================================================ 邮件级

def _check_one_email(
    mail: dict[str, Any],
    findings: list[dict[str, Any]],
    review_flags: list[dict[str, Any]],
) -> dict[str, Any]:
    ident = mail.get("identity") or {}
    uid = mail["uid"]

    from_entries = ident.get("from") or []
    sender_entries = ident.get("sender") or []
    reply_entries = ident.get("reply_to") or []
    rp_entries = ident.get("return_path") or []
    mid_block = ident.get("message_id") or {}
    dkim_entries = ident.get("dkim") or []

    from_first = from_entries[0] if from_entries else None
    sender_first = sender_entries[0] if sender_entries else None
    rp_first = rp_entries[0] if rp_entries else None
    mid_first = (mid_block.get("headers") or [None])[0]

    signals = _context_signals(
        mail, from_entries, sender_entries, reply_entries, review_flags
    )

    # 解析异常 -> 待复核证据（每封邮件内去重）
    _emit_parse_anomalies(mail, ident.get("anomalies") or [], review_flags)

    # 缺失头 -> 待复核证据。From / Message-ID / DKIM-Signature 是三类检查
    # 的依据头，缺失直接影响可核验性；Sender / Reply-To / Return-Path 为
    # 可选头（无 Sender/Return-Path 是最常见的正常情况），缺失只在邮件级
    # 检查报告中体现、不单独刷一条待复核证据，避免噪声
    missing = [
        name
        for name, present in (
            ("From", bool(from_entries)),
            ("Message-ID", mid_block.get("present")),
            ("DKIM-Signature", bool(dkim_entries)),
        )
        if not present
    ]
    if missing:
        _review(
            review_flags, mail, "missing_header",
            headers=missing,
            detail="缺少核验依据头: " + ", ".join(missing),
            evidence={"missing": missing},
        )

    checks: dict[str, Any] = {}

    # ---- 1. From vs Sender / Return-Path 域不一致 -----------------------
    checks["from_domain_mismatch"] = _check_from_mismatch(
        mail, from_first, sender_entries, rp_entries, signals, findings
    )

    # ---- 3a. Message-ID 域 vs From 域（邮件级） -------------------------
    checks["message_id_domain_drift"] = _check_mid_email(
        mail, from_first, mid_first, signals, findings
    )

    # ---- 4. DKIM h= 未覆盖 From -----------------------------------------
    checks["dkim_from_not_covered"] = _check_dkim(
        mail, from_first, dkim_entries, signals, findings
    )

    report = {
        "node_uid": uid,
        "message_id": _mid_value(mid_first),
        "subject": mail.get("subject"),
        "source": mail["source"],
        "from_address": from_first.get("address") if from_first else None,
        "from_domain": from_first.get("domain") if from_first else None,
        "sender_domains": [e.get("domain") for e in sender_entries],
        "return_path_domains": [e.get("domain") for e in rp_entries],
        "reply_to_domains": [e.get("domain") for e in reply_entries],
        "message_id_domain": mid_first.get("domain") if mid_first else None,
        "dkim": [
            {
                "index": d.get("index"),
                "d": d.get("d"),
                "s": d.get("s"),
                "i": d.get("i"),
                "i_domain": d.get("i_domain"),
                "covers_from": d.get("covers_from"),
            }
            for d in dkim_entries
        ],
        "signals": signals,
        "checks": checks,
    }
    return {"report": report, "signals": signals}


def _check_from_mismatch(
    mail: dict[str, Any],
    from_first: dict[str, Any] | None,
    sender_entries: list[dict[str, Any]],
    rp_entries: list[dict[str, Any]],
    signals: list[str],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    headers = ["From"]
    comparisons: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []

    if from_first is None or not from_first.get("address"):
        return {
            "status": "inconclusive",
            "reason": "缺少可解析的 From 头，无法核验 From 与 Sender/Return-Path 域是否一致",
            "comparisons": [],
        }
    from_domain = from_first.get("domain")

    # 重复头（尤其 Return-Path 可能出现多个）的每个值都参与核验，
    # 不能只看首值；每个值给出独立比较单元，附头出现序号 index
    for label, entries in (("Sender", sender_entries), ("Return-Path", rp_entries)):
        if not entries:
            comparisons.append(
                {"header": label, "present": False, "match": None}
            )
            continue
        headers.append(label)
        for entry in entries:
            other_domain = entry.get("domain")
            if other_domain is None:
                comparisons.append(
                    {
                        "header": label,
                        "index": entry.get("index"),
                        "present": True,
                        "address": entry.get("address"),
                        "domain": None,
                        "match": None,
                        "note": "地址无法解析出有效域名",
                    }
                )
                continue
            match = (other_domain == from_domain) if from_domain else None
            cell = {
                "header": label,
                "index": entry.get("index"),
                "present": True,
                "address": entry.get("address"),
                "domain": other_domain,
                "match": match,
            }
            comparisons.append(cell)
            if match is False:
                mismatches.append(cell)

    if not mismatches:
        return {
            "status": "observed" if from_domain else "inconclusive",
            "reason": (
                "From 域可解析，全部 Sender/Return-Path 值未发现域不一致"
                if from_domain
                else "From 地址缺少有效域名，无法比较"
            ),
            "match": True if from_domain else None,
            "comparisons": comparisons,
        }

    status = "needs_review" if signals else "observed"
    finding = {
        "type": "from_domain_mismatch",
        "scope": "email",
        "status": status,
        "node_uids": [mail["uid"]],
        "message_ids": [mail["message_id"]] if mail.get("message_id") else [],
        "sources": _sources(mail),
        "headers": headers,
        "summary": _from_mismatch_summary(from_first, mismatches, signals),
        "basis": "比较 From 与全部 Sender/Return-Path 值（含重复头）首个地址的域名（小写精确比较）",
        "evidence": {
            "from": _address_brief(from_first),
            "comparisons": comparisons,
            "mismatched": mismatches,
            "review_signals": signals,
        },
    }
    findings.append(finding)
    return {
        "status": status,
        "reason": finding["summary"],
        "match": False,
        "comparisons": comparisons,
    }


def _check_mid_email(
    mail: dict[str, Any],
    from_first: dict[str, Any] | None,
    mid_first: dict[str, Any] | None,
    signals: list[str],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    if mid_first is None:
        return {"status": "inconclusive", "reason": "缺少 Message-ID 头，无法核验标识域"}
    if from_first is None or not from_first.get("address"):
        return {"status": "inconclusive", "reason": "缺少可解析的 From 头，无法核验 Message-ID 标识域"}
    mid_domain = mid_first.get("domain")
    from_domain = from_first.get("domain")
    if mid_domain is None or from_domain is None:
        return {
            "status": "inconclusive",
            "reason": "Message-ID 或 From 缺少有效域名，无法比较",
        }
    if mid_domain == from_domain:
        return {
            "status": "observed",
            "reason": "Message-ID 标识域与 From 域一致",
            "message_id_domain": mid_domain,
            "from_domain": from_domain,
            "match": True,
        }

    status = "needs_review" if signals else "observed"
    finding = {
        "type": "message_id_domain_drift",
        "scope": "email",
        "status": status,
        "node_uids": [mail["uid"]],
        "message_ids": [mid_first.get("value")] if mid_first.get("value") else [],
        "sources": _sources(mail),
        "headers": ["Message-ID", "From"],
        "summary": (
            f"Message-ID 标识域 {mid_domain} 与 From 域 {from_domain} 不一致"
            + ("（存在待复核背景: " + "、".join(signals) + "）" if signals else "")
            + "；仅记录域漂移事实，不判定伪造"
        ),
        "basis": "比较 Message-ID <local@domain> 的域部分与 From 地址域（小写精确比较）",
        "evidence": {
            "message_id": mid_first.get("value"),
            "message_id_domain": mid_domain,
            "from": _address_brief(from_first),
            "from_domain": from_domain,
            "review_signals": signals,
        },
    }
    findings.append(finding)
    return {
        "status": status,
        "reason": finding["summary"],
        "message_id_domain": mid_domain,
        "from_domain": from_domain,
        "match": False,
    }


def _check_dkim(
    mail: dict[str, Any],
    from_first: dict[str, Any] | None,
    dkim_entries: list[dict[str, Any]],
    signals: list[str],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    signatures = []
    uncovered: list[dict[str, Any]] = []
    for d in dkim_entries:
        h_fields = d.get("h") or []
        info = {
            "index": d.get("index"),
            "d": d.get("d"),
            "s": d.get("s"),
            "i": d.get("i"),
            "h": h_fields,
            "covers_from": d.get("covers_from"),
            "anomalies": d.get("anomalies") or [],
        }
        signatures.append(info)
        if not d.get("covers_from"):
            uncovered.append(info)

    if not dkim_entries:
        return {
            "status": "inconclusive",
            "reason": "缺少 DKIM-Signature 头，无法核验 h= 是否覆盖 From（不代表无签名即伪造）",
            "signatures": [],
        }

    # From 缺失或不可解析时，无法断言“h= 未覆盖 From”（没有可被覆盖的
    # From）：按证据不足标无法核验，绝不下 observed/未覆盖结论。
    # missing_header 待复核证据已在邮件级检查中记录
    if from_first is None or not from_first.get("address"):
        return {
            "status": "inconclusive",
            "reason": "缺少可解析的 From 头，无法核验 DKIM h= 是否覆盖 From（不判定伪造）",
            "signatures": signatures,
        }

    if not uncovered:
        return {
            "status": "observed",
            "reason": f"{len(dkim_entries)} 个 DKIM 签名的 h= 均覆盖 From（未验证签名真伪）",
            "signatures": signatures,
        }

    # h 缺失/无法解析时无法断言“未覆盖”，标 needs_review；多重签名时
    # 另有签名已覆盖 From，也需人工结合各签名的 d= 复核。转发/列表信号
    # 不改变“h= 未列 From”这一客观事实（其待复核证据另列）
    any_covers = any(d.get("covers_from") for d in dkim_entries)
    unparsable = any(not h for d in uncovered for h in [d.get("h")])
    status = "needs_review" if (any_covers or unparsable) else "observed"
    finding = {
        "type": "dkim_from_not_covered",
        "scope": "email",
        "status": status,
        "node_uids": [mail["uid"]],
        "message_ids": [mail["message_id"]] if mail.get("message_id") else [],
        "sources": _sources(mail),
        "headers": ["DKIM-Signature", "From"],
        "summary": (
            f"{len(uncovered)}/{len(dkim_entries)} 个 DKIM-Signature 的 h= 标签"
            "未覆盖 From"
            + ("（另有签名覆盖，或 h= 无法解析，需人工复核）" if status == "needs_review" else "")
            + "；仅检查覆盖关系，未查 DNS、未验证 b= 签名"
        ),
        "basis": "解析 DKIM-Signature 的 h= 标签字段列表，检查是否列出 from（大小写不敏感）",
        "evidence": {
            "from": _address_brief(from_first) if from_first else None,
            "signatures": signatures,
            "uncovered": uncovered,
            "other_signature_covers_from": any_covers,
            "review_signals": signals,
        },
    }
    findings.append(finding)
    return {
        "status": status,
        "reason": finding["summary"],
        "match": False,
        "signatures": signatures,
    }


# ================================================================ 会话级

def _check_threads(
    emails: list[dict[str, Any]],
    by_uid: dict[int, dict[str, Any]],
    signals_by_uid: dict[int, list[str]],
    findings: list[dict[str, Any]],
    review_flags: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """按会话树汇总回复链身份变化与 Message-ID 域漂移（迭代遍历）。"""
    children: dict[int | None, list[dict[str, Any]]] = {}
    for mail in emails:
        children.setdefault(mail.get("parent_uid"), []).append(mail)
    for siblings in children.values():
        siblings.sort(key=lambda m: (m.get("date") or "", m["uid"]))

    thread_reports: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    # 每个根一棵会话树；栈帧 (node, 祖先链[uid...])
    for root in children.get(None, []):
        stack: list[tuple[dict[str, Any], list[int]]] = [(root, [])]
        thread_uids: list[int] = []
        thread_fobjs: list[dict[str, Any]] = []
        while stack:
            node, ancestors = stack.pop()
            uid = node["uid"]
            thread_uids.append(uid)

            parent = by_uid.get(ancestors[-1]) if ancestors else None
            if parent is not None:
                fobj = _reply_change(
                    node, parent, ancestors, by_uid,
                    signals_by_uid, findings, review_flags,
                )
                if fobj is not None:
                    thread_fobjs.append(fobj)

            for child in reversed(children.get(uid, [])):
                stack.append((child, ancestors + [uid]))

        thread_fobjs.extend(
            _thread_mid_drift(thread_uids, by_uid, signals_by_uid, findings)
        )

        report = {
            "root_uid": root["uid"],
            "root_message_id": root.get("message_id"),
            "root_subject": root.get("subject"),
            "email_count": len(thread_uids),
            "node_uids": sorted(thread_uids),
            "_finding_objs": thread_fobjs,
            "identity_sequences": _identity_sequence(thread_uids, by_uid),
        }
        thread_reports.append((report, thread_fobjs))
    return thread_reports


def _reply_change(
    node: dict[str, Any],
    parent: dict[str, Any],
    ancestors: list[int],
    by_uid: dict[int, dict[str, Any]],
    signals_by_uid: dict[int, list[str]],
    findings: list[dict[str, Any]],
    review_flags: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """回复相对父邮件的身份变化；差异背景（转发/列表）只做待复核。"""
    child_from = _first_from(node)
    parent_from = _first_from(parent)
    if child_from is None or parent_from is None:
        _review(
            review_flags, node, "missing_header",
            headers=["From", "In-Reply-To"],
            detail="回复或父邮件缺少可解析的 From，回复链身份变化无法核验",
            evidence={
                "reply_uid": node["uid"],
                "parent_uid": parent["uid"],
            },
        )
        return None

    child_dom = child_from.get("domain")
    parent_dom = parent_from.get("domain")
    if not child_dom or not parent_dom:
        return None  # 域无法解析，邮件级 missing_header 已记录

    if child_dom == parent_dom:
        return None  # 同域（同部门/同人多地址）不算身份变化

    # 域确实变化：判断“新参与者”还是“既有参与者被替换”
    ancestor_domains = [
        _from_domain(by_uid[a]) for a in ancestors if a in by_uid
    ]
    ancestor_domains = [d for d in ancestor_domains if d]
    # 该域在更早的祖先中出现过 => 新参与者加入线程（常见，observed 也只记录）
    established = len(ancestor_domains) >= 1 and parent_dom in ancestor_domains
    child_seen_before = child_dom in ancestor_domains

    child_signals = set(signals_by_uid.get(node["uid"], []))
    same_display = bool(
        child_from.get("name")
        and child_from.get("name") == parent_from.get("name")
    )

    if child_seen_before:
        # 之前参与过讨论的域再次发言：正常对话轮换，仅低口径记录
        status = "observed"
        reason = "回复域与父邮件不同，但该域此前已参与本会话（对话轮换）"
    elif established and same_display and not child_signals:
        # 父邮件域在链上稳定、显示名相同却换域：最值得警惕的形态
        status = "observed"
        reason = "回复与父邮件显示名相同但域不同，且父域在回复链上稳定出现"
    else:
        status = "needs_review"
        reason = "回复域与父邮件不同，可能为新参与者、转发或邮件列表转发"

    finding = {
        "type": "reply_identity_change",
        "scope": "thread",
        "status": status,
        "node_uids": [node["uid"], parent["uid"]],
        "message_ids": _pair_mids(node, parent),
        "sources": _sources(node) + _sources(parent),
        "headers": ["From", "In-Reply-To", "References"],
        "summary": (
            f"回复链身份变化：父邮件 {parent_from.get('address')} "
            f"({parent_dom}) -> 回复 {child_from.get('address')} "
            f"({child_dom})；{reason}；不判定伪造"
        ),
        "basis": (
            "沿会话父子边比较相邻两封邮件 From 首个地址的域名与显示名，"
            "并参考祖先链上的域出现情况"
        ),
        "evidence": {
            "reply": {"uid": node["uid"], **_address_brief(child_from)},
            "parent": {"uid": parent["uid"], **_address_brief(parent_from)},
            "ancestor_domains": ancestor_domains,
            "same_display_name": same_display,
            "child_domain_seen_earlier": child_seen_before,
            "parent_domain_established": established,
            "review_signals": sorted(child_signals),
        },
    }
    findings.append(finding)
    return finding


def _thread_mid_drift(
    thread_uids: list[int],
    by_uid: dict[int, dict[str, Any]],
    signals_by_uid: dict[int, list[str]],
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """同一会话内，同一 From 地址/显示名的邮件 Message-ID 域漂移。"""
    # 归一化参与者键：优先完整地址；无地址时退化到 显示名@（无把握不猜测）
    groups: dict[str, list[dict[str, Any]]] = {}
    for uid in thread_uids:
        mail = by_uid[uid]
        from_first = _first_from(mail)
        mid_first = _first_mid(mail)
        if from_first is None or mid_first is None:
            continue
        if not from_first.get("address") or mid_first.get("domain") is None:
            continue
        key = from_first["address"].lower()
        groups.setdefault(key, []).append(mail)

    out: list[dict[str, Any]] = []
    for address in sorted(groups):
        mails = groups[address]
        domains = sorted(
            {
                _first_mid(m)["domain"]
                for m in mails
                if _first_mid(m) and _first_mid(m).get("domain")
            }
        )
        if len(domains) <= 1:
            continue
        # 同一会话参与者使用多个 Message-ID 域：需结合客户端/迁移背景复核
        signals = sorted(
            {s for m in mails for s in signals_by_uid.get(m["uid"], [])}
        )
        status = "needs_review"
        cells = []
        for m in mails:
            mid = _first_mid(m)
            cells.append(
                {
                    "uid": m["uid"],
                    "message_id": mid.get("value"),
                    "message_id_domain": mid.get("domain"),
                    "sources": _sources(m),
                }
            )
        finding = {
            "type": "message_id_domain_drift",
            "scope": "thread",
            "status": status,
            "node_uids": sorted(m["uid"] for m in mails),
            "message_ids": [
                c["message_id"] for c in cells if c["message_id"]
            ],
            "sources": [s for m in mails for s in _sources(m)],
            "headers": ["Message-ID", "From"],
            "summary": (
                f"会话内同一发件人 {address} 的 Message-ID 标识域在 "
                f"{', '.join(domains)} 之间漂移（{len(mails)} 封）；"
                "可能为换用邮件服务商/客户端或账号迁移，需人工复核，不判定伪造"
            ),
            "basis": "按 From 地址归组同一会话内的邮件，比较其 Message-ID 域部分",
            "evidence": {
                "from_address": address,
                "domains": domains,
                "emails": cells,
                "review_signals": signals,
            },
        }
        findings.append(finding)
        out.append(finding)
    return out


# ================================================================ 背景信号

def _context_signals(
    mail: dict[str, Any],
    from_entries: list[dict[str, Any]],
    sender_entries: list[dict[str, Any]],
    reply_entries: list[dict[str, Any]],
    review_flags: list[dict[str, Any]],
) -> list[str]:
    """提取转发/邮件列表两类背景信号，并落待复核证据。"""
    signals: list[str] = []

    # 1) 转发：主题前缀（剥离 Re: 后再判 Fwd:）
    subject = mail.get("subject") or ""
    probe = subject
    while True:
        new_probe = _RE_PREFIX_RE.sub("", probe)
        if new_probe == probe:
            break
        probe = new_probe
    if _FORWARD_PREFIX_RE.match(probe):
        signals.append("possible_forward")
        _review(
            review_flags, mail, "possible_forward",
            headers=["Subject"],
            detail=f"主题疑似转发: {subject!r}",
            evidence={"subject": subject},
        )

    # 2) 邮件列表迹象（只使用本模块负责的头，可离线确定）
    list_hints: list[str] = []
    from_first = from_entries[0] if from_entries else None
    from_dom = from_first.get("domain") if from_first else None

    if len(from_entries) > 1 or any(
        len(e.get("addresses") or []) > 1 for e in from_entries
    ):
        list_hints.append("From 含多个地址（RFC 6854 常见于列表/转发）")
    sender_first = sender_entries[0] if sender_entries else None
    if (
        sender_first is not None
        and sender_first.get("address")
        and from_first is not None
        and from_first.get("address")
        and sender_first["address"].lower() != from_first["address"].lower()
    ):
        list_hints.append("存在与 From 不同的 Sender 头（列表代发典型特征）")
    reply_first = reply_entries[0] if reply_entries else None
    if (
        reply_first is not None
        and reply_first.get("domain")
        and from_dom
        and reply_first["domain"] != from_dom
    ):
        list_hints.append("Reply-To 指向与 From 不同的域（列表/代发常见）")
    if len(sender_entries) > 1 or len(reply_entries) > 1:
        list_hints.append("Sender/Reply-To 出现重复头")

    if list_hints:
        signals.append("possible_mailing_list")
        _review(
            review_flags, mail, "possible_mailing_list",
            headers=["From", "Sender", "Reply-To"],
            detail="；".join(list_hints),
            evidence={"hints": list_hints},
        )

    return signals


def _emit_parse_anomalies(
    mail: dict[str, Any],
    anomalies: list[dict[str, Any]],
    review_flags: list[dict[str, Any]],
) -> None:
    seen: set[tuple[Any, Any, str]] = set()
    for anom in anomalies:
        key = (anom.get("header"), anom.get("index"), anom.get("detail"))
        if key in seen:
            continue
        seen.add(key)
        _review(
            review_flags, mail, "header_parse_anomaly",
            headers=[_header_display(anom.get("header"))],
            detail=anom.get("detail", "头字段解析异常"),
            evidence={
                "kind": anom.get("kind"),
                "header": anom.get("header"),
                "index": anom.get("index"),
                "raw": anom.get("raw"),
            },
        )


# ================================================================ 工具

def _review(
    review_flags: list[dict[str, Any]],
    mail: dict[str, Any],
    kind: str,
    headers: list[str],
    detail: str,
    evidence: dict[str, Any],
) -> None:
    review_flags.append(
        {
            "kind": kind,
            "node_uids": [mail["uid"]],
            "message_ids": [mail["message_id"]] if mail.get("message_id") else [],
            "sources": _sources(mail),
            "headers": headers,
            "detail": detail,
            "basis": detail,
            "evidence": evidence,
        }
    )


def _first_from(mail: dict[str, Any]) -> dict[str, Any] | None:
    entries = (mail.get("identity") or {}).get("from") or []
    return entries[0] if entries else None


def _first_mid(mail: dict[str, Any]) -> dict[str, Any] | None:
    block = (mail.get("identity") or {}).get("message_id") or {}
    headers = block.get("headers") or []
    return headers[0] if headers else None


def _from_domain(uid_mail: dict[str, Any]) -> str | None:
    first = _first_from(uid_mail)
    return first.get("domain") if first else None


def _mid_value(mid_first: dict[str, Any] | None) -> str | None:
    return mid_first.get("value") if mid_first else None


def _address_brief(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "address": entry.get("address"),
        "name": entry.get("name"),
        "domain": entry.get("domain"),
    }


def _sources(mail: dict[str, Any]) -> list[dict[str, Any]]:
    source = mail.get("source") or {}
    return [source] if source else []


def _pair_mids(node: dict[str, Any], parent: dict[str, Any]) -> list[str]:
    out = []
    for mail in (node, parent):
        mid = mail.get("message_id")
        if mid and mid not in out:
            out.append(mid)
    return out


def _from_mismatch_summary(
    from_first: dict[str, Any],
    mismatches: list[dict[str, Any]],
    signals: list[str],
) -> str:
    def label(cell: dict[str, Any]) -> str:
        idx = cell.get("index")
        return f"{cell['header']}#{idx}" if idx is not None else cell["header"]

    parts = [
        f"{label(cell)} 域 {cell['domain']} ≠ From 域 {from_first.get('domain')}"
        for cell in mismatches
    ]
    text = "From 与 " + "；".join(parts)
    if signals:
        text += "（待复核背景: " + "、".join(signals) + "）"
    return text + "；仅记录声明域差异，不判定伪造"


def _identity_sequence(
    thread_uids: list[int], by_uid: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """会话身份序列：按树顺序给出每封邮件的 From / 标识域，便于人工浏览。"""
    seq = []
    for uid in thread_uids:
        mail = by_uid[uid]
        first = _first_from(mail)
        mid = _first_mid(mail)
        seq.append(
            {
                "uid": uid,
                "from": _address_brief(first),
                "message_id_domain": mid.get("domain") if mid else None,
            }
        )
    return seq


_HEADER_DISPLAY = {
    "from": "From",
    "sender": "Sender",
    "reply-to": "Reply-To",
    "return-path": "Return-Path",
    "message-id": "Message-ID",
    "dkim-signature": "DKIM-Signature",
}


def _header_display(name: str | None) -> str:
    return _HEADER_DISPLAY.get(name or "", name or "?")


def _reorder_key_first(items: list[dict[str, Any]], key: str) -> None:
    """原地把每个字典的某个键挪到最前（JSON 输出更易读）。"""
    for index, item in enumerate(items):
        items[index] = {
            key: item[key],
            **{k: v for k, v in item.items() if k != key},
        }


def _build_stats(
    emails: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    review_flags: list[dict[str, Any]],
    email_rows: list[dict[str, Any]],
    thread_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    by_type = {t: 0 for t in FINDING_TYPES}
    by_status = {"observed": 0, "needs_review": 0, "inconclusive": 0}
    for f in findings:
        by_type[f["type"]] += 1
        by_status[f["status"]] = by_status.get(f["status"], 0) + 1

    by_review = {k: 0 for k in REVIEW_KINDS}
    for flag in review_flags:
        by_review[flag["kind"]] = by_review.get(flag["kind"], 0) + 1

    def count_check(check: str, status: str) -> int:
        return sum(
            1
            for row in email_rows
            if row["report"]["checks"].get(check, {}).get("status") == status
        )

    inconclusive_checks = {
        t: count_check(t, "inconclusive")
        for t in (
            "from_domain_mismatch",
            "message_id_domain_drift",
            "dkim_from_not_covered",
        )
    }

    return {
        "emails": len(emails),
        "thread_count": len(thread_rows),
        "findings_total": len(findings),
        "findings_by_type": by_type,
        "findings_by_status": by_status,
        "review_flags_total": len(review_flags),
        "review_flags_by_kind": by_review,
        "inconclusive_checks": inconclusive_checks,
    }
