"""单封 .eml 邮件的解析。

输出稳定的字典结构，任何无法解析/解码的问题都进入 ``issues`` 列表，
不向调用方抛异常——单封坏邮件不应导致整个作业失败，原始文件也永不修改。
"""

from __future__ import annotations

import email
import hashlib
import re
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import default
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from .idheaders import parse_identity_headers
from .received import parse_received_headers

# 从裸文本里提取 <msg-id@host> 形态标识
_MSGID_RE = re.compile(r"<[^<>@\s]+@[^<>\s]+>")
# References 里偶见没有尖括号的 token，兜底拆分
_TOKEN_SPLIT_RE = re.compile(r"[\s,]+")


def parse_eml(raw: bytes, source_name: str) -> dict[str, Any]:
    """解析一封邮件的原始字节。

    返回字段见模块文档；``issues`` 为人类可读问题字符串列表。
    """
    issues: list[str] = []
    raw_sha = hashlib.sha256(raw).hexdigest()

    try:
        msg = email.message_from_bytes(raw, policy=default)
    except Exception as exc:  # 极端畸形结构
        # 即便 MIME 结构无法按默认策略解析，仍尽力用宽松策略保留身份头
        try:
            identity = parse_identity_headers(email.message_from_bytes(raw))
        except Exception:
            identity = _empty_identity()
        return {
            "source_file": source_name,
            "raw_sha256": raw_sha,
            "message_id": None,
            "in_reply_to": None,
            "references": [],
            "date": None,
            "from": None,
            "to": [],
            "cc": [],
            "subject": None,
            "body_text": "",
            "body_html_present": False,
            "attachments": [],
            "received": [],
            "identity": identity,
            "issues": [f"MIME 结构无法解析: {type(exc).__name__}: {exc}"],
        }

    # 解析器在遇到畸形头/编码时会记录 defect
    for defect in msg.defects:
        issues.append(f"邮件级解析缺陷: {type(defect).__name__}")

    message_id = _first_msgid(msg.get("Message-ID", ""), issues, "Message-ID")
    in_reply_to = _first_msgid(msg.get("In-Reply-To", ""), issues, "In-Reply-To")
    references = _extract_references(msg.get("References", ""), issues)

    date_iso = None
    raw_date = msg.get("Date")
    if raw_date:
        try:
            dt = parsedate_to_datetime(raw_date)
            if dt is not None:
                date_iso = dt.isoformat()
        except (TypeError, ValueError, IndexError) as exc:
            issues.append(f"Date 头无法解析 ({raw_date!r}): {exc}")

    subject = None
    try:
        subject = msg.get("Subject")
    except Exception as exc:  # 畸形编码头
        issues.append(f"Subject 头无法解码: {type(exc).__name__}: {exc}")

    body_text, html_present, body_issues = _extract_body(msg)
    issues.extend(body_issues)

    attachments, att_issues = _extract_attachments(msg)
    issues.extend(att_issues)

    # Received 头全部保留（原始顺序），逐跳解析时间/主机；问题只进该跳的
    # issues，不阻断解析
    received = parse_received_headers(msg.get_all("Received", []))

    # 声明身份头（From/Sender/Reply-To/Return-Path/Message-ID/DKIM-Signature）：
    # 保留重复头与原始值，只提取地址/域名与 d/s/i/h 标签，不核验真伪
    identity = parse_identity_headers(msg)

    return {
        "source_file": source_name,
        "raw_sha256": raw_sha,
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "references": references,
        "date": date_iso,
        "from": _parse_address_header(msg.get("From")),
        "to": _parse_address_list(msg.get_all("To", [])),
        "cc": _parse_address_list(msg.get_all("Cc", [])),
        "subject": subject,
        "body_text": body_text,
        "body_html_present": html_present,
        "attachments": attachments,
        "received": received,
        "identity": identity,
        "issues": issues,
    }


def _empty_identity() -> dict[str, Any]:
    return {
        "from": [],
        "sender": [],
        "reply_to": [],
        "return_path": [],
        "message_id": {"present": False, "headers": []},
        "dkim": [],
        "anomalies": [],
    }


# ---------------------------------------------------------------- 引用头

def _first_msgid(value: str, issues: list[str], header: str) -> str | None:
    if not value:
        return None
    match = _MSGID_RE.search(value)
    if match:
        return match.group(0)
    token = value.strip().split()[0] if value.strip() else ""
    if "@" in token:  # 没有尖括号但形态像 msg-id，兜底接受并记录
        issues.append(f"{header} 头缺少尖括号，已按原值接受: {token!r}")
        return token
    return None


def _extract_references(value: str, issues: list[str]) -> list[str]:
    if not value:
        return []
    ids = _MSGID_RE.findall(value)
    # 记录下无尖括号、疑似标识的 token
    leftover = _MSGID_RE.sub(" ", value)
    for token in _TOKEN_SPLIT_RE.split(leftover):
        if "@" in token:
            issues.append(f"References 中存在无尖括号标识，已忽略: {token!r}")
    # 去重保序
    seen: set[str] = set()
    result: list[str] = []
    for mid in ids:
        if mid not in seen:
            seen.add(mid)
            result.append(mid)
    return result


# ---------------------------------------------------------------- 地址

def _format_address(addr: Address) -> dict[str, str] | None:
    try:
        email_addr = addr.addr_spec or ""
        display = addr.display_name or ""
    except Exception:
        return None
    if not email_addr and not display:
        return None
    return {"name": display, "address": email_addr}


def _parse_address_header(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        pairs = getaddresses([value])
    else:  # email.policy 解析出的 AddressHeader
        try:
            return _format_address(value.addresses[0]) if value.addresses else None
        except Exception:
            pairs = getaddresses([str(value)])
    if not pairs or not any(p[1] for p in pairs):
        return None
    name, addr = pairs[0]
    return {"name": name or "", "address": addr or ""}


def _parse_address_list(values: list[Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for value in values:
        if isinstance(value, str):
            for name, addr in getaddresses([value]):
                if addr:
                    result.append({"name": name or "", "address": addr})
        else:
            try:
                for addr in value.addresses:
                    formatted = _format_address(addr)
                    if formatted:
                        result.append(formatted)
            except Exception:
                for name, addr in getaddresses([str(value)]):
                    if addr:
                        result.append({"name": name or "", "address": addr})
    # 同地址去重保序
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for item in result:
        if item["address"] not in seen:
            seen.add(item["address"])
            unique.append(item)
    return unique


# ---------------------------------------------------------------- 正文

def _decode_part_bytes(
    part: EmailMessage, issues: list[str], what: str
) -> str | None:
    """按声明字符集解码文本部件，失败时逐级降级。"""
    payload = part.get_payload(decode=True)
    if payload is None:
        issues.append(f"{what}: 无法取得传输解码后的字节")
        return None
    charset = None
    try:
        charset = part.get_content_charset()
    except Exception:
        pass

    candidates = []
    if charset:
        candidates.append(charset)
    candidates.extend(["utf-8", "gb18030", "latin-1"])

    seen: set[str] = set()
    for enc in candidates:
        key = enc.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            text = payload.decode(enc)
            if enc != (charset or "").lower() and charset:
                issues.append(
                    f"{what}: 声明字符集 {charset!r} 解码失败，"
                    f"已使用 {enc} 降级解码"
                )
            return text
        except (UnicodeDecodeError, LookupError, TypeError):
            continue
    issues.append(f"{what}: 所有候选字符集均无法解码，正文已置空")
    return None


def _extract_body(msg: EmailMessage) -> tuple[str, bool, list[str]]:
    issues: list[str] = []
    html_present = False
    chosen: EmailMessage | None = None
    try:
        chosen = msg.get_body(preferencelist=("plain", "html"))
    except Exception as exc:
        issues.append(f"遍历 MIME 正文失败: {type(exc).__name__}: {exc}")

    if chosen is None:
        return "", False, issues

    try:
        html_present = chosen.get_content_type() == "text/html"
    except Exception:
        html_present = False

    what = "HTML 正文" if html_present else "文本正文"
    for defect in chosen.defects:
        issues.append(f"{what}解析缺陷: {type(defect).__name__}")

    text = _decode_part_bytes(chosen, issues, what)
    if text is None:
        return "", html_present, issues
    if html_present:
        text = _html_to_text(text)
    return text.strip(), True if (html_present or text) else False, issues


_TAG_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_BR_RE = re.compile(
    r"(?i)<\s*/?\s*(br|p|div|tr|h[1-6]|li|ul|ol|table|blockquote)[^>]*>"
)
_TAG_STRIP_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _html_to_text(html: str) -> str:
    """极简 HTML 转文本（不引入第三方依赖），仅用于留档正文。"""
    import html as html_mod

    text = _TAG_RE.sub("", html)
    text = _BR_RE.sub("\n", text)
    text = _TAG_STRIP_RE.sub("", text)
    text = html_mod.unescape(text)
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line is not None)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------- 附件

# 传输编码损坏类缺陷（base64 非法字符/长度/填充）：解析器会尽力给出字节，
# 但结果不可信，附件流转追踪时按“内容无法解码”处理
_DECODE_DEFECT_NAMES = {
    "InvalidBase64CharactersDefect",
    "InvalidBase64LengthDefect",
    "InvalidBase64PaddingDefect",
}


def _extract_attachments(
    msg: EmailMessage,
) -> tuple[list[dict[str, Any]], list[str]]:
    attachments: list[dict[str, Any]] = []
    issues: list[str] = []
    try:
        parts = list(msg.iter_attachments())
    except Exception as exc:
        issues.append(f"枚举附件失败: {type(exc).__name__}: {exc}")
        return attachments, issues

    used_names: dict[str, int] = {}
    for index, part in enumerate(parts):
        try:
            content_type = part.get_content_type()
        except Exception:
            content_type = "application/octet-stream"
        filename = None
        try:
            filename = part.get_filename()
        except Exception as exc:
            issues.append(f"附件 #{index + 1} 文件名解码失败: {exc}")
        if not filename:
            filename = f"unnamed-{index + 1}.bin"

        # undecodable=True 表示内容无法可靠解码：此时记录的 size/sha256
        # 只是占位值，附件流转追踪不得用它推断流转（只列为待复核）
        undecodable = False
        try:
            payload = part.get_payload(decode=True)
        except Exception as exc:
            issues.append(
                f"附件 {filename!r} 传输解码失败: "
                f"{type(exc).__name__}: {exc}"
            )
            payload = None
            undecodable = True
        if payload is None:
            payload = b""
            undecodable = True
            issues.append(f"附件 {filename!r} 内容无法解码，大小按 0 记录")
        elif not isinstance(payload, (bytes, bytearray)):
            # 防御：传输解码未给出字节，无法计算可靠哈希
            payload = b""
            undecodable = True
            issues.append(
                f"附件 {filename!r} 内容无法解码（载荷非字节），大小按 0 记录"
            )

        decode_defects = [
            type(defect).__name__
            for defect in part.defects
            if type(defect).__name__ in _DECODE_DEFECT_NAMES
        ]
        if decode_defects and not undecodable:
            undecodable = True
            issues.append(
                f"附件 {filename!r} 传输编码损坏"
                f"（{', '.join(decode_defects)}），无法解码出可靠内容"
            )

        for defect in part.defects:
            issues.append(
                f"附件 {filename!r} 解析缺陷: {type(defect).__name__}"
            )

        # 同名附件去重命名，仅为元数据展示用
        unique_name = filename
        if filename in used_names:
            used_names[filename] += 1
            stem, dot, ext = filename.rpartition(".")
            prefix = stem if dot else filename
            suffix = f".{ext}" if dot else ""
            unique_name = f"{prefix}({used_names[filename]}){suffix}"
        else:
            used_names[filename] = 0

        attachment = {
            "filename": unique_name,
            "content_type": content_type,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        if undecodable:
            attachment["undecodable"] = True
        attachments.append(attachment)
    return attachments, issues
