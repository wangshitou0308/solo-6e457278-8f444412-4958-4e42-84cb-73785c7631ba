"""邮件声明身份相关头的解析：保留重复头与原始值，只提取、不核验真伪。

覆盖六个头字段：``From``、``Sender``、``Reply-To``、``Return-Path``、
``Message-ID`` 与 ``DKIM-Signature``。与本项目其他解析层一致的原则：

* **重复头全部保留**：每个字段都按在邮件中的原始出现顺序给出
  ``headers``（含 ``index`` 与逐字 ``raw``，折叠空白保留为原始形态）；
* **只提取、不判断**：提取地址、域名以及 DKIM 的 ``d``/``s``/``i``/``h``
  标签；**不查询 DNS，不校验 DKIM 签名（``b=``）真伪，不评价身份是否
  可信**；
* 无法解析、字段畸形、标签缺失/重复等情况只写入 ``anomalies``，
  对应提取结果置空，绝不猜测或补全。

输出结构::

    {
        "from":       [ {index, raw, address, name, domain, anomalies} ],
        "sender":     [ ... ],
        "reply_to":   [ ... ],
        "return_path":[ ... ],
        "message_id": { "present": bool, "headers": [ {index, raw, value,
                        local_part, domain, anomalies}] },
        "dkim":       [ {index, raw, present, d, s, i, i_domain,
                         h, fields, anomalies} ],
        "anomalies":  [ {kind, header, index, detail, raw?}, ... ]
    }

``raw`` 保留逐字头值（包含原始折叠换行 CRLF/空白）；标签解析使用去折叠
后的文本，不影响原始值留存。
"""

from __future__ import annotations

import re
from email.message import Message
from typing import Any

# 关注的身份头（规范化小写名 -> 输出键）。注意 Return-Path 带连字符
_ADDRESS_HEADERS = (
    ("from", "from"),
    ("sender", "sender"),
    ("reply-to", "reply_to"),
    ("return-path", "return_path"),
)

# DKIM-Signature 中按本模块需求提取的标签（RFC 6376）
_DKIM_TAG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# DKIM h= 标签中的头名分隔符（冒号分隔，允许空白折叠）
_HSPLIT_RE = re.compile(r"[\s:]+")
# 尖括号包裹（<msg-id@host>、<return-path@host>，含空 <>）
_ANGLE_RE = re.compile(r"<(.*?)>", re.DOTALL)
# 去折叠：CRLF/LF 后的折叠空白一并规范化为空格
_FOLD_RE = re.compile(r"\r?\n[ \t]+")


def parse_identity_headers(msg: Message) -> dict[str, Any]:
    """从已解析的邮件对象中提取声明身份头信息。"""
    # raw_items 给出未解码、保留折叠的原始 (name, value)，重复头逐行出现。
    # 无法按头声明编码解码的字节会以 surrogateescape 形式残留，这里统一
    # 用替换字符兜底，保证原始值可留档、可 JSON 序列化（不猜测原文）。
    try:
        raw_items = [
            (_safe_text(name), _safe_text(value))
            for name, value in msg.raw_items()
        ]
    except Exception:
        raw_items = []

    # 按出现顺序收集目标头（name 统一小写比较，保留原始名仅用于留痕）。
    # 桶键使用邮件中的真实头名（return-path 带连字符），输出键另做映射。
    buckets: dict[str, list[tuple[int, str]]] = {
        header: [] for header, _key in _ADDRESS_HEADERS
    }
    buckets["message-id"] = []
    buckets["dkim-signature"] = []
    for raw_name, raw_value in raw_items:
        lower = raw_name.lower()
        if lower in buckets:
            buckets[lower].append((raw_value, raw_name))

    anomalies: list[dict[str, Any]] = []

    result: dict[str, Any] = {}
    for header_name, key in _ADDRESS_HEADERS:
        entries = []
        for index, (raw_value, _orig_name) in enumerate(buckets[header_name]):
            entries.append(_parse_address_entry(header_name, index, raw_value, anomalies))
        # RFC 5322：From/Reply-To 可由多个地址构成（允许重复头），
        # 但 Sender/Return-Path 应只出现一次；重复出现时全部值保留并
        # 参与核验，同时记录 duplicate_header 异常（不做取舍）
        if len(entries) > 1 and header_name in ("sender", "return-path"):
            display = {
                "from": "From", "sender": "Sender",
                "reply-to": "Reply-To", "return-path": "Return-Path",
            }.get(header_name, header_name)
            anomalies.append(
                _anomaly(
                    "duplicate_header",
                    header_name,
                    None,
                    f"出现 {len(entries)} 个 {display} 头（按 RFC 应只有一个），"
                    "已全部保留并参与核验、未做取舍",
                )
            )
        result[key] = entries

    result["message_id"] = _parse_message_id(buckets["message-id"], anomalies)
    result["dkim"] = _parse_dkim_headers(buckets["dkim-signature"], anomalies)
    result["anomalies"] = anomalies
    return result


# ---------------------------------------------------------------- 地址头

def _parse_address_entry(
    header: str,
    index: int,
    raw_value: str,
    anomalies: list[dict[str, Any]],
) -> dict[str, Any]:
    """解析单个地址头；地址头按 RFC 应只有一个地址，多出的并列保留。"""
    entry_anoms: list[str] = []
    unfolded = _unfold(raw_value).strip()

    # 先按策略解析（display_name 会完成 RFC 2047 编码字解码），再退回
    # getaddresses，保证显示名可读；raw 仍保留逐字原值
    addresses = _addresses_with_policy(unfolded)
    if addresses is None:
        pairs = getaddresses([unfolded])
        addresses = [
            {"name": name or "", "address": addr or "", "domain": _domain_of(addr)}
            for name, addr in pairs
            if addr or name
        ]
    for item in addresses:
        if item["address"] and item["domain"] is None:
            entry_anoms.append(f"地址 {item['address']!r} 不含有效域名部分")

    if not unfolded:
        entry_anoms.append("头值为空")
    elif not addresses:
        entry_anoms.append("无法从头值中解析出任何地址")
    elif len(addresses) > 1 and header in ("from", "sender", "return-path"):
        # RFC 5322：Sender/Return-Path 单地址；From 可多地址（group），
        # 多地址 From 常见于邮件列表/转发，记录但不报错
        if header in ("sender", "return-path"):
            entry_anoms.append(
                f"{header} 头应只含一个地址，实际解析出 {len(addresses)} 个"
            )

    first = addresses[0] if addresses else None
    entry = {
        "index": index,
        "raw": raw_value,
        "count": len(addresses),
        "addresses": addresses,
        # 便捷字段：首个地址（本项目其余结构按单地址建模）
        "address": first["address"] if first else None,
        "name": first["name"] if first else None,
        "domain": first["domain"] if first else None,
        "anomalies": entry_anoms,
    }
    for detail in entry_anoms:
        anomalies.append(_anomaly("malformed_value", header, index, detail, raw_value))
    return entry


def _addresses_with_policy(value: str) -> list[dict[str, Any]] | None:
    """用 default 策略解析地址头（含编码字显示名解码）；失败返回 None。"""
    from email import policy

    try:
        header = policy.default.header_factory("from", value)
        addresses_attr = getattr(header, "addresses", None)
        if addresses_attr is None:
            return None
        result: list[dict[str, Any]] = []
        for addr in addresses_attr:  # type: Address
            spec = getattr(addr, "addr_spec", "") or ""
            display = getattr(addr, "display_name", "") or ""
            if not spec and not display:
                continue
            result.append(
                {
                    "name": display,
                    "address": spec,
                    "domain": _domain_of(spec),
                }
            )
        return result
    except Exception:
        return None


# ---------------------------------------------------------------- Message-ID

def _parse_message_id(
    headers: list[tuple[str, str]],
    anomalies: list[dict[str, Any]],
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for index, (raw_value, _orig_name) in enumerate(headers):
        entry_anoms: list[str] = []
        unfolded = _unfold(raw_value).strip()
        value: str | None = None
        local_part: str | None = None
        domain: str | None = None

        if not unfolded:
            entry_anoms.append("Message-ID 头值为空")
        else:
            # 优先提取 <...>；多个尖括号段时取第一个疑似标识
            angle = _ANGLE_RE.search(unfolded)
            candidate = angle.group(1).strip() if angle else unfolded.split()[0]
            candidate = candidate.strip()
            if "@" not in candidate:
                entry_anoms.append(
                    f"Message-ID 不含 '@' 域名部分，原值: {unfolded!r}"
                )
                value = f"<{candidate}>" if angle else candidate
            else:
                local, dom = candidate.rsplit("@", 1)
                if not local or not dom:
                    entry_anoms.append(
                        f"Message-ID 形态畸形，原值: {unfolded!r}"
                    )
                domain = dom.lower() if dom else None
                local_part = local or None
                value = f"<{candidate}>" if angle else candidate
                if not angle:
                    entry_anoms.append(
                        f"Message-ID 缺少尖括号包裹，已按原值接受: {candidate!r}"
                    )
                # 尖括号内不允许空白；折叠/空白是畸形信号
                if re.search(r"\s", candidate):
                    entry_anoms.append(
                        f"Message-ID 标识内含有空白，原值: {unfolded!r}"
                    )

        entries.append(
            {
                "index": index,
                "raw": raw_value,
                "value": value,
                "local_part": local_part,
                "domain": domain,
                "anomalies": entry_anoms,
            }
        )
        for detail in entry_anoms:
            anomalies.append(
                _anomaly("malformed_value", "message-id", index, detail, raw_value)
            )

    if len(entries) > 1:
        anomalies.append(
            _anomaly(
                "duplicate_header",
                "message-id",
                None,
                f"出现 {len(entries)} 个 Message-ID 头，已全部保留、未做取舍",
            )
        )
    return {"present": bool(entries), "headers": entries}


# ---------------------------------------------------------------- DKIM

def parse_dkim_signature(raw_value: str) -> dict[str, Any]:
    """解析单个 DKIM-Signature 头值的 d/s/i/h 标签（不校验签名）。

    供 :func:`parse_identity_headers` 与单测复用；``raw`` 由调用方留存。
    """
    anomalies: list[str] = []
    unfolded = _unfold(raw_value).strip()
    h_fields: list[str] = []

    # tag-list：标签用 ';' 分隔，每个 spec 为 tag=value
    tags: dict[str, str] = {}
    duplicate_tags: list[str] = []
    for spec in unfolded.split(";"):
        piece = spec.strip()
        if not piece:
            continue
        if "=" not in piece:
            anomalies.append(f"存在不含 '=' 的标签段: {piece!r}")
            continue
        tag, _, val = piece.partition("=")
        tag = tag.strip()
        val = val.strip()
        if not _DKIM_TAG_RE.match(tag):
            anomalies.append(f"标签名非法: {tag!r}")
            continue
        tag_l = tag.lower()
        if tag_l in tags:
            if tag_l not in duplicate_tags:
                duplicate_tags.append(tag_l)
            # RFC 6376：重复标签名的签名非法；保留首值，记录异常
            continue
        tags[tag_l] = val
    for tag in duplicate_tags:
        anomalies.append(f"标签 {tag!r} 重复出现，签名按 RFC 6376 视为非法，已保留首个值")

    # v 标签应为 1（DKIM-Signature）
    if "v" not in tags:
        anomalies.append("缺少 v 标签")
    elif tags["v"] != "1":
        anomalies.append(f"v 标签值非 1: {tags['v']!r}")

    d = _required_tag(tags, "d", anomalies)
    s = _required_tag(tags, "s", anomalies)
    i = tags.get("i")
    if i is not None:
        # i 身份为 local-part@domain 或纯域；域部分必须与 d 同域或子域
        i_local: str | None
        i_domain: str | None
        if "@" in i:
            i_local, _, i_dom_raw = i.rpartition("@")
            i_local = i_local or None
        else:
            i_local, i_dom_raw = None, i
        i_domain = i_dom_raw.lower() or None
        if d and i_domain and not _domain_related(i_domain, d.lower()):
            anomalies.append(
                f"i= 身份域 {i_domain!r} 与 d= 签名域 {d.lower()!r} 不一致"
                "（仅记录，不判断签名真伪）"
            )
    else:
        i_local = None
        i_domain = None

    h_raw = tags.get("h")
    if h_raw is None:
        anomalies.append("缺少 h= 标签，无法判断签名覆盖了哪些头字段")
    else:
        seen: set[str] = set()
        for token in _HSPLIT_RE.split(h_raw):
            name = token.strip().lower()
            if not name:
                continue
            # h= 允许重复列出同一字段；保留出现序列（重复有签名学含义），
            # 覆盖判断只看是否出现过
            h_fields.append(name)
            seen.add(name)
        if not h_fields:
            anomalies.append(f"h= 标签未列出任何字段，原值: {h_raw!r}")

    # b= 是签名字段本身：只记录是否存在，绝不尝试验证
    if "b" not in tags:
        anomalies.append("缺少 b= 签名字段（仅记录，不验证签名）")

    covers_from = "from" in set(h_fields)
    return {
        "tags": {"d": d, "s": s, "i": i, "h": h_raw},
        "d": d,
        "s": s,
        "i": i,
        "i_local_part": i_local,
        "i_domain": i_domain,
        "h": h_fields,
        "covers_from": covers_from,
        "anomalies": anomalies,
    }


def _parse_dkim_headers(
    headers: list[tuple[str, str]],
    anomalies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, (raw_value, _orig_name) in enumerate(headers):
        parsed = parse_dkim_signature(raw_value)
        entry = {
            "index": index,
            "raw": raw_value,
            "present": True,
            "d": parsed["d"],
            "s": parsed["s"],
            "i": parsed["i"],
            "i_local_part": parsed["i_local_part"],
            "i_domain": parsed["i_domain"],
            "h": parsed["h"],
            "covers_from": parsed["covers_from"],
            "anomalies": parsed["anomalies"],
        }
        entries.append(entry)
        for detail in parsed["anomalies"]:
            anomalies.append(
                _anomaly("dkim_tag_error", "dkim-signature", index, detail, raw_value)
            )
    if len(entries) > 1:
        anomalies.append(
            _anomaly(
                "duplicate_header",
                "dkim-signature",
                None,
                f"出现 {len(entries)} 个 DKIM-Signature 头（可能为多重签名），"
                "已全部保留、未做取舍",
            )
        )
    return entries


# ---------------------------------------------------------------- 工具

def _required_tag(
    tags: dict[str, str], tag: str, anomalies: list[str]
) -> str | None:
    val = tags.get(tag)
    if val is None:
        anomalies.append(f"缺少 {tag}= 标签")
        return None
    if not val:
        anomalies.append(f"{tag}= 标签值为空")
        return None
    return val


def _domain_related(child: str, parent: str) -> bool:
    """child 是否等于 parent 或为其子域（仅字符串比较，不查 DNS）。"""
    return child == parent or child.endswith("." + parent)


def _domain_of(address: str) -> str | None:
    """从裸地址提取小写域名；非法形态返回 None（不猜测）。"""
    if not address or "@" not in address:
        return None
    domain = address.rsplit("@", 1)[1].strip()
    if not domain or "." not in domain:
        return None
    return domain.lower()


def _unfold(value: str) -> str:
    """RFC 头去折叠：折叠换行（CRLF/LF + WSP）替换为单个空格。"""
    return _FOLD_RE.sub(" ", value.replace("\r\n", "\n").replace("\n", " "))


def _safe_text(value: Any) -> str:
    """把可能带 surrogateescape 残留的头值转为可 UTF-8 序列化的字符串。"""
    if not isinstance(value, str):
        value = str(value)
    return value.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def _anomaly(
    kind: str,
    header: str,
    index: int | None,
    detail: str,
    raw: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": kind,
        "header": header,
        "index": index,
        "detail": detail,
    }
    if raw is not None:
        item["raw"] = raw
    return item
