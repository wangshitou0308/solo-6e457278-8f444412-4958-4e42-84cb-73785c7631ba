"""Received 头解析：按原顺序保留全部跳点，提取时间/发送主机/接收主机。

每封邮件的 Received 头由沿途服务器**逐条前置**，因此文件中出现的顺序
是“最后一跳在最上方”。本模块只负责解析与标注，不做任何猜测：

* 时间统一换算为 UTC（``time_utc``），同时保留原始时区标记
  （``timezone``）与原始时区下的本地时间（``time_original``）；
* 缺少时区、时间无法解析、缺少 from/by 子句等情况都写入该跳的
  ``issues``，对应字段置 ``None``，绝不假设或补全。

输出结构（每跳一个字典，``index`` 为在邮件头中的出现顺序，0 = 最上方）::

    {
        "index": 0,
        "raw": "from a.example by b.example; Mon, 01 Sep 2026 09:00:12 +0200",
        "from_host": "a.example" | None,
        "by_host": "b.example" | None,
        "time_utc": "2026-09-01T07:00:12+00:00" | None,
        "time_original": "2026-09-01T09:00:12+02:00" | None,
        "timezone": "+0200" | None,
        "issues": ["..."]
    }
"""

from __future__ import annotations

import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any

# 括号注释（Received 的 from/by 子句常带 "(helo [ip])" 注释，先剥离再匹配）
_COMMENT_RE = re.compile(r"\([^()]*\)")
# from / by 子句的主机 token（注释剥离后取第一个非空白词）
_FROM_RE = re.compile(r"\bfrom\s+([^\s;]+)", re.IGNORECASE)
_BY_RE = re.compile(r"\bby\s+([^\s;]+)", re.IGNORECASE)
# 时区标记：+HHMM / -HHMM，或 RFC 2822 过时命名时区（GMT/UT/EST/...）
_ZONE_RE = re.compile(r"([+-]\d{4}|[A-Za-z]{1,5})\s*$")


def parse_received_headers(values: list[Any]) -> list[dict[str, Any]]:
    """解析全部 Received 头，按原始出现顺序返回跳点列表。"""
    hops: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        raw = str(value).replace("\r\n", " ").replace("\n", " ")
        raw = re.sub(r"[ \t]+", " ", raw).strip()
        hops.append(_parse_one(index, raw))
    return hops


def _parse_one(index: int, raw: str) -> dict[str, Any]:
    issues: list[str] = []
    hop: dict[str, Any] = {
        "index": index,
        "raw": raw,
        "from_host": None,
        "by_host": None,
        "time_utc": None,
        "time_original": None,
        "timezone": None,
        "issues": issues,
    }

    # ---- 拆分“传输子句 ; 日期时间”（日期固定在最后一个分号之后） --------
    if ";" not in raw:
        clause, date_part = raw, ""
        issues.append("缺少 ';' 分隔的日期时间部分，本跳时间无法确定")
    else:
        clause, date_part = raw.rsplit(";", 1)
        date_part = date_part.strip()
        if not date_part:
            issues.append("';' 之后的日期时间部分为空，本跳时间无法确定")

    # ---- from / by 主机（剥离括号注释后匹配，不猜测缺失项） -------------
    cleaned = _strip_comments(clause)
    from_match = _FROM_RE.search(cleaned)
    if from_match:
        hop["from_host"] = from_match.group(1).rstrip(",;")
    else:
        issues.append("缺少 from 子句，发送主机未知")
    by_match = _BY_RE.search(cleaned)
    if by_match:
        hop["by_host"] = by_match.group(1).rstrip(",;")
    else:
        issues.append("缺少 by 子句，接收主机未知")

    # ---- 日期时间：保留原始时区标记，仅在时区明确时换算 UTC -------------
    if date_part:
        _parse_datetime(date_part, hop)

    return hop


def _strip_comments(text: str) -> str:
    """剥离括号注释（少数头存在嵌套括号，迭代至稳定，限 10 轮）。"""
    for _ in range(10):
        stripped = _COMMENT_RE.sub(" ", text)
        if stripped == text:
            break
        text = stripped
    return text


def _parse_datetime(date_part: str, hop: dict[str, Any]) -> None:
    issues: list[str] = hop["issues"]
    try:
        dt = parsedate_to_datetime(date_part)
    except (TypeError, ValueError, IndexError, OverflowError) as exc:
        issues.append(
            f"日期时间无法解析 ({date_part!r}): {exc}；本跳不参与时序比较"
        )
        return
    if dt is None:
        issues.append(f"日期时间无法解析 ({date_part!r})；本跳不参与时序比较")
        return

    # 原始时区标记：去掉尾部括号注释后取最后一个 token；仅在日期本身
    # 可解析时提取（不可解析时整串都是证据，不单独标注时区）
    zone_source = _strip_comments(date_part).strip()
    zone_match = _ZONE_RE.search(zone_source)
    zone_token = zone_match.group(1) if zone_match else None

    if dt.tzinfo is None:
        # 缺时区（或使用了无法识别的命名时区）：不做任何时区假设
        hop["time_original"] = dt.isoformat()
        if zone_token and zone_token.isalpha():
            hop["timezone"] = zone_token
            issues.append(
                f"时区标记 {zone_token!r} 无法识别，未换算 UTC，"
                "本跳不参与时序比较"
            )
        else:
            issues.append("日期时间缺少时区，未换算 UTC，本跳不参与时序比较")
        return

    hop["timezone"] = zone_token
    hop["time_original"] = dt.isoformat()
    hop["time_utc"] = dt.astimezone(timezone.utc).isoformat()
