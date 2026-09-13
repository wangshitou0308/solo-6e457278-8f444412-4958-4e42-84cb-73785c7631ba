"""正文引文结构解析（解析期保留 ``>`` 层级、回复分隔线与 ``<blockquote>`` 边界）。

纯文本与 HTML 正文都会被切分为**连续片段（segment）**，每个片段记录：

* ``kind`` — ``own``（本人文字）/ ``quote``（引文）/ ``forward``（转发内容）/
  ``separator``（分隔线）；
* ``level`` — 引用层级（0 = 本人文字；``>`` 每嵌套一层 +1，HTML
  ``<blockquote>`` 每嵌套一层 +1）；
* ``marker`` — 边界标记来源：``">"``、``"blockquote"``、``"on_wrote"``、
  ``"cn_daowen"``、``"original_message"``、``"forwarded_message"``、
  ``"outlook_header"``、``"signature"``，组合以 ``+`` 连接
  （如 ``"blockquote+>"``）；含糊分隔线的 ``marker`` 为 ``null``；
* ``ambiguous`` — 片段是否处于**含糊分隔线**之后（边界不可靠）；
* ``char_start`` / ``char_end`` — 片段在正文视图中的字符区间（含 ``>`` 等
  原始前缀）；``text`` 为片段原始文本（**不去除**引用前缀），
  片段按行边界无缝拼接可完整还原正文视图。

正文视图（offset basis）：

* 纯文本正文：视图即 ``body_text`` 本身（``offset_basis="body_text"``）；
* HTML 正文：先按 ``<blockquote>`` 边界切分，再逐块做与留档正文一致的
  HTML→文本转换，按块拼成视图（``offset_basis="html_view"``），
  视图文本可由片段 ``text`` 以 ``"\\n"`` 连接精确还原。

本模块只做结构切分，不做任何来源归属判断；对齐与标注由 quoteflow 完成。
"""

from __future__ import annotations

import re
from typing import Any

STRUCTURE_VERSION = 1

# ---------------------------------------------------------------- HTML→文本

_TAG_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_BR_RE = re.compile(
    r"(?i)<\s*/?\s*(br|p|div|tr|h[1-6]|li|ul|ol|table|blockquote)[^>]*>"
)
_TAG_STRIP_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def html_to_text(html: str) -> str:
    """极简 HTML 转文本（不引入第三方依赖），仅用于留档正文/引文视图。"""
    import html as html_mod

    text = _TAG_RE.sub("", html)
    text = _BR_RE.sub("\n", text)
    text = _TAG_STRIP_RE.sub("", text)
    text = html_mod.unescape(text)
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line is not None)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------- 行分类

# 引用前缀：行首至多 3 个空白后跟一个或多个 ">"（"> > " 计两层）
_QUOTE_PREFIX_RE = re.compile(r"^[ \t]{0,3}((?:>[ \t]?)+)")

# 严格回复分隔线（marker, 进入的模式)；模式："quote"=之后为引文，
# "forward"=之后为转发内容，"reset"=结束当前模式（签名线之后为本人签名），
# "keep"=不改变当前模式
_ON_WROTE_RE = re.compile(r"^On\s.{0,200}?\bwrote\s*[:：]\s*$")
_CN_WROTE_RE = re.compile(r"^在\s*.{0,120}?写道\s*[:：]\s*$")
_ORIGINAL_MESSAGE_RE = re.compile(
    r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE
)
_FORWARDED_MESSAGE_RE = re.compile(
    r"^\s*-{2,}\s*(Forwarded message|转发的邮件)\s*-{2,}\s*$", re.IGNORECASE
)
_SIGNATURE_RE = re.compile(r"^--\s?$")

# Outlook 原文头块：From:/发件人： 起，后跟 ≥2 个字段行（Sent:/To:/Subject: 等）
_OUTLOOK_FROM_RE = re.compile(r"^(From|发件人)\s*[:：]\s*\S", re.IGNORECASE)
_OUTLOOK_FIELD_RE = re.compile(
    r"^(From|Sent|Date|To|Cc|Subject|发件人|发送时间|日期|收件人|抄送|主题)"
    r"\s*[:：]",
    re.IGNORECASE,
)
_OUTLOOK_MAX_FIELDS = 8

# 含糊分隔线：看似分隔但无法可靠归类（只列待复核，不做结构断言）
_AMBIG_RULE_RE = re.compile(r"^\s*[-–—_=*~]{4,}\s*$")
_AMBIG_DASHED_TEXT_RE = re.compile(r"^\s*[-–—]{2,}\s*\S.*\S\s*[-–—]{2,}\s*$")
_AMBIG_ON_WROTE_RE = re.compile(r"^On\s.{0,200}?\bwrote\.?\s*$", re.IGNORECASE)
_AMBIG_CN_WROTE_RE = re.compile(r"^在\s*.{0,120}?写道\s*$")

# <blockquote> 边界（容忍属性与大小写）
_BLOCKQUOTE_RE = re.compile(r"(?i)<\s*(/?)\s*blockquote\b[^>]*>")


def strip_quote_prefix(line: str) -> tuple[int, str, int]:
    """剥离行首引用前缀，返回 ``(层级, 内容, 前缀字符数)``。"""
    match = _QUOTE_PREFIX_RE.match(line)
    if not match:
        return 0, line, 0
    return match.group(1).count(">"), line[match.end():], match.end()


def empty_structure(offset_basis: str = "body_text") -> dict[str, Any]:
    return {
        "version": STRUCTURE_VERSION,
        "offset_basis": offset_basis,
        "segments": [],
    }


def _strict_separator(line: str) -> tuple[str, str] | None:
    """严格分隔线：返回 ``(marker, 模式)``；签名线模式为 ``"keep"``。"""
    if _ON_WROTE_RE.match(line):
        return "on_wrote", "quote"
    if _CN_WROTE_RE.match(line):
        return "cn_daowen", "quote"
    if _ORIGINAL_MESSAGE_RE.match(line):
        return "original_message", "forward"
    if _FORWARDED_MESSAGE_RE.match(line):
        return "forwarded_message", "forward"
    if _SIGNATURE_RE.match(line):
        return "signature", "reset"
    return None


def _is_ambiguous_separator(line: str) -> bool:
    """含糊分隔线：形态接近分隔线但不满足任何严格模式。"""
    if _AMBIG_RULE_RE.match(line):
        return True
    if _AMBIG_DASHED_TEXT_RE.match(line):
        return True
    if _AMBIG_ON_WROTE_RE.match(line):
        return True
    if _AMBIG_CN_WROTE_RE.match(line):
        return True
    return False


def _outlook_block_len(lines: list[str], start: int) -> int:
    """Outlook 原文头块行数（From/发件人 行 + 连续字段行 ≥2），否则 0。"""
    if not _OUTLOOK_FROM_RE.match(lines[start]):
        return 0
    fields = 0
    pos = start + 1
    while (
        pos < len(lines)
        and fields < _OUTLOOK_MAX_FIELDS
        and _OUTLOOK_FIELD_RE.match(lines[pos])
    ):
        fields += 1
        pos += 1
    return 1 + fields if fields >= 2 else 0


def _compose_marker(base_marker: str | None, extra: str | None) -> str | None:
    parts = [part for part in (base_marker, extra) if part]
    return "+".join(parts) if parts else None


def _segment_lines(
    lines: list[str],
    *,
    base_level: int = 0,
    base_marker: str | None = None,
    start_offset: int = 0,
    first_index: int = 0,
) -> list[dict[str, Any]]:
    """把一组行切分为片段（核心状态机，纯文本与 HTML 块共用）。

    ``base_level`` / ``base_marker`` 为 HTML ``<blockquote>`` 提供的基准
    层级与标记；``start_offset`` 为首个行在正文视图中的偏移。
    """
    # ---- 1. 逐行分类为 cell ------------------------------------------------
    # cell: ("line", kind, level, marker, ambiguous)
    #       ("separator", marker, ambiguous, span)  span>1 时为 Outlook 头块
    #       ("blank",)
    cells: list[tuple] = []
    mode: str | None = None          # None | "quote" | "forward"
    mode_marker: str | None = None   # 进入 quote 模式的分隔线 marker
    ambiguous_mode = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            cells.append(("blank",))
            index += 1
            continue
        block_len = _outlook_block_len(lines, index)
        if block_len:
            cells.append(("separator", "outlook_header", False, block_len))
            for _ in range(block_len - 1):
                cells.append(("skip",))
            mode, mode_marker = "forward", None
            ambiguous_mode = False
            index += block_len
            continue
        strict = _strict_separator(line)
        if strict:
            marker, new_mode = strict
            cells.append(("separator", marker, False, 1))
            if new_mode == "reset":
                mode, mode_marker = None, None
            elif new_mode != "keep":
                mode = new_mode
                mode_marker = marker if new_mode == "quote" else None
                ambiguous_mode = False
            index += 1
            continue
        if _is_ambiguous_separator(line):
            cells.append(("separator", None, True, 1))
            ambiguous_mode = True
            index += 1
            continue
        level, _, _ = strip_quote_prefix(line)
        if level > 0:
            kind = "forward" if mode == "forward" else "quote"
            cells.append(
                (
                    "line",
                    kind,
                    base_level + level,
                    _compose_marker(base_marker, ">"),
                    ambiguous_mode,
                )
            )
        elif mode == "forward":
            cells.append(
                ("line", "forward", base_level, base_marker, ambiguous_mode)
            )
        elif mode == "quote":
            cells.append(
                (
                    "line",
                    "quote",
                    base_level + 1,
                    _compose_marker(base_marker, mode_marker),
                    ambiguous_mode,
                )
            )
        elif base_level > 0:
            # blockquote 内的普通行即被引用内容
            cells.append(
                ("line", "quote", base_level, base_marker, ambiguous_mode)
            )
        else:
            cells.append(("line", "own", 0, None, ambiguous_mode))
        index += 1

    # ---- 2. 行偏移 ---------------------------------------------------------
    line_offsets: list[int] = []
    offset = start_offset
    for line in lines:
        line_offsets.append(offset)
        offset += len(line) + 1  # 以 "\n" 切分，换行符占 1 字符

    # ---- 3. 归并连续同类 cell 为片段 ---------------------------------------
    segments: list[dict[str, Any]] = []
    cur_key: tuple | None = None
    cur_first = 0
    cur_last = -1

    def flush() -> None:
        nonlocal cur_key, cur_first, cur_last
        if cur_key is None:
            return
        kind, level, marker, ambiguous = cur_key
        text = "\n".join(lines[cur_first:cur_last + 1])
        segments.append(
            {
                "index": first_index + len(segments),
                "kind": kind,
                "level": level,
                "marker": marker,
                "ambiguous": ambiguous,
                "char_start": line_offsets[cur_first],
                "char_end": line_offsets[cur_last] + len(lines[cur_last]),
                "text": text,
            }
        )
        cur_key = None

    pending_blanks = 0  # 当前无组时缓存的空行数（归入下一组）
    pos = 0
    while pos < len(cells):
        cell = cells[pos]
        if cell[0] == "skip":
            pos += 1
            continue
        if cell[0] == "separator":
            flush()
            pending_blanks = 0
            _, marker, ambiguous, span = cell
            last = pos + span - 1
            segments.append(
                {
                    "index": first_index + len(segments),
                    "kind": "separator",
                    "level": 0,
                    "marker": marker,
                    "ambiguous": ambiguous,
                    "char_start": line_offsets[pos],
                    "char_end": line_offsets[last] + len(lines[last]),
                    "text": "\n".join(lines[pos:last + 1]),
                }
            )
            pos += span
            continue
        if cell[0] == "blank":
            if cur_key is None:
                pending_blanks += 1
            else:
                cur_last = pos  # 空行并入当前组
            pos += 1
            continue
        # 普通内容行
        _, kind, level, marker, ambiguous = cell
        key = (kind, level, marker, ambiguous)
        if key != cur_key:
            flush()
            cur_key = key
            cur_first = pos - pending_blanks
            pending_blanks = 0
        cur_last = pos
        pos += 1
    flush()
    # 末尾残留空行（正文以空行结尾且无内容组）：归入本人文字
    if pending_blanks:
        first = len(lines) - pending_blanks
        segments.append(
            {
                "index": first_index + len(segments),
                "kind": "own",
                "level": base_level,
                "marker": base_marker,
                "ambiguous": False,
                "char_start": line_offsets[first],
                "char_end": line_offsets[-1] + len(lines[-1]),
                "text": "\n".join(lines[first:]),
            }
        )
    return segments


def structure_from_text(body_text: str) -> dict[str, Any]:
    """纯文本正文的引文结构；字符区间直接落在 ``body_text`` 上。"""
    if not body_text:
        return empty_structure()
    segments = _segment_lines(body_text.split("\n"))
    return {
        "version": STRUCTURE_VERSION,
        "offset_basis": "body_text",
        "segments": segments,
    }


def structure_from_html(html: str) -> dict[str, Any]:
    """HTML 正文的引文结构，保留 ``<blockquote>`` 嵌套边界。

    视图文本 = 各 ``<blockquote>`` 深度块的转换文本以 ``"\\n"`` 连接；
    字符区间落在该视图上（``offset_basis="html_view"``）。
    """
    if not html:
        return empty_structure("html_view")
    cleaned = _TAG_RE.sub("", html)

    # 按 <blockquote> 开/闭标签切分，记录每块的嵌套深度
    fragments: list[tuple[int, str]] = []
    depth = 0
    pos = 0
    for match in _BLOCKQUOTE_RE.finditer(cleaned):
        fragments.append((depth, cleaned[pos:match.start()]))
        if match.group(1) == "/":
            depth = max(0, depth - 1)  # 闭合多于开启时不产生负层级
        else:
            depth += 1
        pos = match.end()
    fragments.append((depth, cleaned[pos:]))

    segments: list[dict[str, Any]] = []
    view_len = 0
    for frag_depth, frag_html in fragments:
        text = html_to_text(frag_html)
        if not text:
            continue
        frag_segments = _segment_lines(
            text.split("\n"),
            base_level=frag_depth,
            base_marker="blockquote" if frag_depth > 0 else None,
            start_offset=view_len,
            first_index=len(segments),
        )
        segments.extend(frag_segments)
        view_len += len(text) + 1  # 块间以 "\n" 连接
    if not segments:
        return empty_structure("html_view")
    return {
        "version": STRUCTURE_VERSION,
        "offset_basis": "html_view",
        "segments": segments,
    }


def view_text(structure: dict[str, Any], body_text: str) -> str:
    """还原引文结构对应的正文视图文本（匹配与区间定位的基准）。"""
    if structure.get("offset_basis") == "body_text":
        return body_text
    return "\n".join(
        segment["text"] for segment in structure.get("segments", [])
    )
