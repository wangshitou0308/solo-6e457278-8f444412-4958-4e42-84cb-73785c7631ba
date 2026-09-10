"""邮件传输时序核验引擎。

结合 ``Date`` 头、``Received`` 跳点链与会话父子关系，按用户配置的阈值
标出时序异常。原则与解析层一致：**只标注、不猜测**——缺时区、无法解析、
相邻跳逆序等情况都写入结论的 ``evidence`` 与说明，不补全、不修正。

核验项（``type``）：

* ``client_clock_skew`` — 客户端时钟偏差：``Date`` 与首跳（路径方向上
  最早一跳，即邮件头中最下方那条 Received）接收时间的差值超过
  ``clock_skew_seconds``；
* ``abnormal_transit`` — 异常传输耗时：相邻两跳（均带可用 UTC 时间）的
  间隔超过 ``max_transit_seconds``；
* ``hop_time_inversion`` — 相邻跳时间逆序（后一跳反而更早），不猜测
  原因，并列两跳证据；
* ``reply_before_parent`` — 回复邮件的 ``Date`` 早于父邮件超过
  ``clock_skew_seconds`` 容差；
* ``chain_mismatch`` — 同一 Message-ID 在不同来源（不同内容）中的
  Received 传输链不一致，各版本传输链并列展示。

每项结论都记录 ``fields``（所用字段）与 ``thresholds``（本次判定使用的
阈值），便于审计复核。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# 结论类型（用于 timeline 接口的 type 筛选校验）
FINDING_TYPES = (
    "client_clock_skew",
    "abnormal_transit",
    "hop_time_inversion",
    "reply_before_parent",
    "chain_mismatch",
)


def analyze(emails: list[dict[str, Any]], thresholds: dict[str, int]) -> dict[str, Any]:
    """对扁平邮件列表做时序核验。

    ``emails`` 每项需包含：``uid``、``message_id``、``subject``、
    ``from``、``date``（ISO 或 None）、``received``（跳点列表）、
    ``parent_uid``（会话父节点 uid 或 None）、``source``（来源描述）。

    返回 ``{"findings": [...], "timeline": [...], "stats": {...}}``。
    """
    skew_limit = thresholds["clock_skew_seconds"]
    transit_limit = thresholds["max_transit_seconds"]

    findings: list[dict[str, Any]] = []
    by_uid = {mail["uid"]: mail for mail in emails}

    # 每封邮件的可比时间（UTC datetime 或 None），供多项检查复用
    date_utc: dict[int, datetime | None] = {}
    for mail in emails:
        date_utc[mail["uid"]] = _aware_from_iso(mail.get("date"))

    for mail in emails:
        uid = mail["uid"]
        hops = mail.get("received") or []
        # 路径方向：文件中最后一条 Received 是最早的一跳
        path_hops = list(reversed(hops))
        first_hop = path_hops[0] if path_hops else None

        # ---- 1. 客户端时钟偏差：Date vs 首跳接收时间 -------------------
        mail_date = date_utc[uid]
        first_time = _hop_time(first_hop)
        if mail_date is not None and first_time is not None:
            skew = (first_time - mail_date).total_seconds()
            if abs(skew) > skew_limit:
                findings.append(
                    {
                        "type": "client_clock_skew",
                        "time": _iso(mail_date),
                        "node_uids": [uid],
                        "message_ids": _mids(mail),
                        "summary": (
                            f"Date 头与首跳接收时间相差 "
                            f"{_fmt_seconds(skew)}，超过时钟偏差阈值 "
                            f"{skew_limit} 秒，疑似客户端时钟"
                            f"{'偏慢' if skew > 0 else '偏快'}"
                        ),
                        "fields": ["Date", "Received"],
                        "thresholds": {"clock_skew_seconds": skew_limit},
                        "evidence": {
                            "date_header": mail.get("date"),
                            "first_hop": _hop_brief(first_hop),
                            "skew_seconds": skew,
                            "direction": (
                                "client_behind" if skew > 0 else "client_ahead"
                            ),
                        },
                    }
                )

        # ---- 2/3. 相邻跳传输耗时与逆序 ---------------------------------
        for earlier, later in zip(path_hops, path_hops[1:]):
            t1 = _hop_time(earlier)
            t2 = _hop_time(later)
            if t1 is None or t2 is None:
                continue  # 缺可用时间的跳不参与比较（原因在该跳 issues 中）
            gap = (t2 - t1).total_seconds()
            if gap < 0:
                findings.append(
                    {
                        "type": "hop_time_inversion",
                        "time": _iso(t1),
                        "node_uids": [uid],
                        "message_ids": _mids(mail),
                        "summary": (
                            f"传输路径上相邻两跳时间逆序：第 "
                            f"{earlier['index']} 跳 "
                            f"({earlier.get('by_host') or '未知主机'}) 的时间 "
                            f"{_iso(t1)} 晚于路径上其后到达的第 "
                            f"{later['index']} 跳 "
                            f"({later.get('by_host') or '未知主机'}) 的时间 "
                            f"{_iso(t2)}，逆序 {_fmt_seconds(-gap)}"
                            "（跳编号按 Received 头出现顺序）；"
                            "不猜测原因，两跳原始证据并列"
                        ),
                        "fields": ["Received"],
                        "thresholds": {},
                        "evidence": {
                            "gap_seconds": gap,
                            "earlier_hop": _hop_brief(earlier),
                            "later_hop": _hop_brief(later),
                        },
                    }
                )
            elif gap > transit_limit:
                findings.append(
                    {
                        "type": "abnormal_transit",
                        "time": _iso(t1),
                        "node_uids": [uid],
                        "message_ids": _mids(mail),
                        "summary": (
                            f"传输路径上第 {earlier['index']} 跳 → 第 "
                            f"{later['index']} 跳耗时 {_fmt_seconds(gap)}，"
                            f"超过传输耗时阈值 {transit_limit} 秒"
                            "（跳编号按 Received 头出现顺序）"
                        ),
                        "fields": ["Received"],
                        "thresholds": {"max_transit_seconds": transit_limit},
                        "evidence": {
                            "gap_seconds": gap,
                            "earlier_hop": _hop_brief(earlier),
                            "later_hop": _hop_brief(later),
                        },
                    }
                )

        # ---- 4. 回复早于父邮件 ------------------------------------------
        parent_uid = mail.get("parent_uid")
        if parent_uid is not None and parent_uid in by_uid:
            parent = by_uid[parent_uid]
            child_date = date_utc[uid]
            parent_date = date_utc[parent_uid]
            if child_date is not None and parent_date is not None:
                diff = (parent_date - child_date).total_seconds()
                if diff > skew_limit:
                    findings.append(
                        {
                            "type": "reply_before_parent",
                            "time": _iso(child_date),
                            "node_uids": [uid, parent_uid],
                            "message_ids": _mids(mail) + _mids(parent),
                            "summary": (
                                f"回复的 Date 早于父邮件 "
                                f"{_fmt_seconds(diff)}，超过时钟偏差容差 "
                                f"{skew_limit} 秒"
                            ),
                            "fields": ["Date", "In-Reply-To", "References"],
                            "thresholds": {"clock_skew_seconds": skew_limit},
                            "evidence": {
                                "reply": _mail_brief(mail),
                                "parent": _mail_brief(parent),
                                "diff_seconds": diff,
                            },
                        }
                    )

    # ---- 5. 同一 Message-ID 的传输链不一致（并列展示） ------------------
    findings.extend(_chain_mismatches(emails))

    # 结论编号（按节点顺序生成，稳定可复核）
    for seq, finding in enumerate(findings, start=1):
        finding["id"] = f"F{seq:04d}"
    # 响应字段顺序：id 放最前
    findings = [
        {"id": f["id"], **{k: v for k, v in f.items() if k != "id"}}
        for f in findings
    ]

    timeline = _build_timeline(emails, date_utc, findings)
    stats = _build_stats(emails, findings)
    return {"findings": findings, "timeline": timeline, "stats": stats}


# ---------------------------------------------------------------- 内部：检查 5

def _chain_mismatches(emails: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一 Message-ID、不同内容的邮件，Received 链不一致时并列展示。"""
    by_mid: dict[str, list[dict[str, Any]]] = {}
    for mail in emails:
        mid = mail.get("message_id")
        if mid:
            by_mid.setdefault(mid, []).append(mail)

    findings: list[dict[str, Any]] = []
    for mid in sorted(by_mid):
        group = by_mid[mid]
        # 按内容（SHA-256）归并版本：字节相同的邮件传输链必然一致
        variants: dict[str, list[dict[str, Any]]] = {}
        for mail in group:
            variants.setdefault(mail.get("raw_sha256") or "", []).append(mail)
        if len(variants) < 2:
            continue

        # 规范化传输链（文件原顺序）：(from_host, by_host, time_utc)
        chains: dict[str, list[tuple[Any, ...]]] = {}
        for sha, mails in variants.items():
            hops = mails[0].get("received") or []
            chains[sha] = [
                (h.get("from_host"), h.get("by_host"), h.get("time_utc"))
                for h in hops
            ]
        distinct_chains = {tuple(chain) for chain in chains.values()}
        if len(distinct_chains) < 2:
            continue  # 各版本传输链一致，无需并列

        variant_list: list[dict[str, Any]] = []
        for sha in sorted(variants, key=lambda s: variants[s][0]["uid"]):
            mails = variants[sha]
            variant_list.append(
                {
                    "raw_sha256": sha or None,
                    "node_uids": [m["uid"] for m in mails],
                    "sources": [m["source"] for m in mails],
                    "hop_count": len(mails[0].get("received") or []),
                    "hops": mails[0].get("received") or [],
                }
            )
        node_uids = [m["uid"] for m in group]
        findings.append(
            {
                "type": "chain_mismatch",
                "time": None,
                "node_uids": node_uids,
                "message_ids": [mid],
                "summary": (
                    f"Message-ID {mid} 对应 {len(variants)} 个内容不同的"
                    f"版本，Received 传输链不一致（"
                    + "；".join(_chain_diffs(chains, variants))
                    + "），各版本传输链并列展示，不做取舍"
                ),
                "fields": ["Message-ID", "Received"],
                "thresholds": {},
                "evidence": {
                    "message_id": mid,
                    "variant_count": len(variants),
                    "variants": variant_list,
                },
            }
        )
    return findings


def _chain_diffs(
    chains: dict[str, list[tuple[Any, ...]]],
    variants: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """生成各版本传输链差异的人类可读摘要（证据见 variants）。"""
    diffs: list[str] = []
    shas = sorted(chains, key=lambda s: variants[s][0]["uid"])
    counts = [len(chains[s]) for s in shas]
    if len(set(counts)) > 1:
        diffs.append("跳数不同: " + " vs ".join(str(c) for c in counts))
        return diffs
    for pos in range(counts[0] if counts else 0):
        cells = [chains[s][pos] for s in shas]
        if len(set(cells)) <= 1:
            continue
        labels = ("发送主机", "接收主机", "时间")
        for field_idx, label in enumerate(labels):
            values = {c[field_idx] for c in cells}
            if len(values) > 1:
                shown = " vs ".join(repr(c[field_idx]) for c in cells)
                diffs.append(f"第 {pos} 跳{label}不同: {shown}")
                break
        if len(diffs) >= 5:  # 摘要限长，完整证据在 variants 中
            break
    return diffs or ["传输链内容不同"]


# ---------------------------------------------------------------- 内部：时间线

def _build_timeline(
    emails: list[dict[str, Any]],
    date_utc: dict[int, datetime | None],
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """每封邮件一个时间线条目，按定位时间升序（无可用时间的排最后）。"""
    finding_ids_by_uid: dict[int, list[str]] = {}
    for finding in findings:
        for uid in finding["node_uids"]:
            finding_ids_by_uid.setdefault(uid, []).append(finding["id"])

    entries: list[dict[str, Any]] = []
    for mail in emails:
        uid = mail["uid"]
        hops = mail.get("received") or []
        usable_date = date_utc[uid]
        # 定位时间：优先 Date（时区可用时），否则最早一跳的 UTC 时间
        first_hop_time = _hop_time(hops[-1]) if hops else None
        if usable_date is not None:
            time_value, basis = _iso(usable_date), "date"
        elif first_hop_time is not None:
            time_value, basis = _iso(first_hop_time), "received"
        else:
            time_value, basis = None, None

        notes: list[str] = []
        if mail.get("date") and usable_date is None:
            notes.append(
                "Date 头缺少时区，未换算 UTC，不参与时钟偏差与回复时序比较"
            )
        if not mail.get("date"):
            notes.append("缺少 Date 头，时钟偏差与回复时序检查未执行")
        for hop in hops:
            for issue in hop.get("issues", []):
                notes.append(f"第 {hop['index']} 跳: {issue}")

        entries.append(
            {
                "node_uid": uid,
                "message_id": mail.get("message_id"),
                "subject": mail.get("subject"),
                "from": mail.get("from"),
                "date": mail.get("date"),
                "time": time_value,
                "time_basis": basis,
                "hop_count": len(hops),
                "source": mail["source"],
                "finding_ids": finding_ids_by_uid.get(uid, []),
                "notes": notes,
            }
        )

    entries.sort(
        key=lambda e: (e["time"] is None, e["time"] or "", e["node_uid"])
    )
    return entries


def _build_stats(
    emails: list[dict[str, Any]], findings: list[dict[str, Any]]
) -> dict[str, Any]:
    by_type = {finding_type: 0 for finding_type in FINDING_TYPES}
    for finding in findings:
        by_type[finding["type"]] += 1
    hops_total = 0
    hops_with_issues = 0
    with_received = 0
    for mail in emails:
        hops = mail.get("received") or []
        if hops:
            with_received += 1
        hops_total += len(hops)
        hops_with_issues += sum(1 for h in hops if h.get("issues"))
    return {
        "emails": len(emails),
        "emails_with_received": with_received,
        "hops_total": hops_total,
        "hops_with_issues": hops_with_issues,
        "findings_total": len(findings),
        "findings_by_type": by_type,
    }


# ---------------------------------------------------------------- 内部：工具

def _aware_from_iso(value: Any) -> datetime | None:
    """ISO 字符串 -> 带时区 datetime；缺失/无时区/非法一律返回 None。"""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None  # 无时区不猜测
    return dt


def _hop_time(hop: dict[str, Any] | None) -> datetime | None:
    if not hop:
        return None
    return _aware_from_iso(hop.get("time_utc"))


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _mids(mail: dict[str, Any]) -> list[str]:
    mid = mail.get("message_id")
    return [mid] if mid else []


def _hop_brief(hop: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": hop.get("index"),
        "from_host": hop.get("from_host"),
        "by_host": hop.get("by_host"),
        "time_utc": hop.get("time_utc"),
        "timezone": hop.get("timezone"),
    }


def _mail_brief(mail: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": mail["uid"],
        "message_id": mail.get("message_id"),
        "date": mail.get("date"),
        "source": mail["source"],
    }


def _fmt_seconds(seconds: float) -> str:
    """秒数的人类可读形式（整数秒去小数点）。"""
    if seconds == int(seconds):
        return f"{int(seconds)} 秒"
    return f"{seconds:.1f} 秒"
