"""不受 Python 递归深度限制的 JSON 解析与序列化。

标准库 ``json.loads`` / ``json.dumps`` 按值递归，遇到 1000+ 层嵌套的
会话树会抛 ``RecursionError``。本模块用显式栈实现等价功能：

* :func:`loads` 支持完整 JSON（对象/数组/字符串转义含代理对/数字/literal）；
* :func:`dumps` 支持本服务用到的 dict/list/str/int/float/bool/None，
  行为对齐 ``json.dumps(ensure_ascii=..., indent=...)``。

均为线性时间、堆内存与深度成正比，深度 1 万级也可正常工作。
"""

from __future__ import annotations

from typing import Any

# ================================================================ 解析

_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


class JsonDecodeError(ValueError):
    pass


def loads(text: str | bytes) -> Any:
    """迭代式 JSON 解析。"""
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    n = len(text)
    pos = 0

    def skip_ws() -> None:
        nonlocal pos
        while pos < n and text[pos] in " \t\r\n":
            pos += 1

    def parse_string() -> str:
        # 进入时 text[pos] == '"'
        nonlocal pos
        pos += 1
        chars: list[str] = []
        while pos < n:
            ch = text[pos]
            if ch == '"':
                pos += 1
                return "".join(chars)
            if ch == "\\":
                pos += 1
                if pos >= n:
                    break
                esc = text[pos]
                if esc == "u":
                    code = _hex4(text, pos + 1)
                    pos += 5
                    if 0xD800 <= code <= 0xDBFF:
                        # 高代理项，尝试配对低代理项
                        if (
                            pos + 5 < n
                            and text[pos] == "\\"
                            and text[pos + 1] == "u"
                        ):
                            low = _hex4(text, pos + 2)
                            if 0xDC00 <= low <= 0xDFFF:
                                pos += 6
                                code = 0x10000 + (
                                    (code - 0xD800) << 10
                                ) + (low - 0xDC00)
                        chars.append(chr(code))
                    else:
                        chars.append(chr(code))
                else:
                    mapped = _ESCAPES.get(esc)
                    if mapped is None:
                        raise JsonDecodeError(
                            f"非法字符串转义 \\{esc}，位置 {pos}"
                        )
                    chars.append(mapped)
                    pos += 1
            else:
                if ord(ch) < 0x20:
                    raise JsonDecodeError(
                        f"字符串内出现未转义控制字符，位置 {pos}"
                    )
                chars.append(ch)
                pos += 1
        raise JsonDecodeError("字符串未正常结束")

    def parse_number() -> tuple[int | float, int]:
        start = pos
        p = pos
        if p < n and text[p] == "-":
            p += 1
        if p >= n or not text[p].isdigit():
            raise JsonDecodeError(f"非法数字，位置 {start}")
        if text[p] == "0":
            p += 1
        else:
            while p < n and text[p].isdigit():
                p += 1
        is_float = False
        if p < n and text[p] == ".":
            is_float = True
            p += 1
            frac = p
            while p < n and text[p].isdigit():
                p += 1
            if p == frac:
                raise JsonDecodeError(f"小数部分缺失，位置 {start}")
        if p < n and text[p] in "eE":
            is_float = True
            p += 1
            if p < n and text[p] in "+-":
                p += 1
            exp = p
            while p < n and text[p].isdigit():
                p += 1
            if p == exp:
                raise JsonDecodeError(f"指数部分缺失，位置 {start}")
        token = text[start:p]
        return (float(token) if is_float else int(token)), p

    def read_value() -> Any:
        """读一个值。

        标量返回 ("s", python 值)；容器返回可变帧（list）压入栈。
        帧结构：["a", 容器] 或 ["o", 容器, 状态, 待写键]，
        对象状态：key/colon/value/comma。
        """
        nonlocal pos
        ch = text[pos]
        if ch == '"':
            return ("s", parse_string())
        if ch == "{":
            pos += 1
            return ["o", {}, "key", None]
        if ch == "[":
            pos += 1
            return ["a", [], "start", None]
        if ch == "t":
            if not text.startswith("true", pos):
                raise JsonDecodeError(f"非法字面量，位置 {pos}")
            pos += 4
            return ("s", True)
        if ch == "f":
            if not text.startswith("false", pos):
                raise JsonDecodeError(f"非法字面量，位置 {pos}")
            pos += 5
            return ("s", False)
        if ch == "n":
            if not text.startswith("null", pos):
                raise JsonDecodeError(f"非法字面量，位置 {pos}")
            pos += 4
            return ("s", None)
        if ch == "-" or ch.isdigit():
            value, new_pos = parse_number()
            pos = new_pos
            return ("s", value)
        raise JsonDecodeError(f"意外字符 {ch!r}，位置 {pos}")

    def assign(value: Any) -> None:
        if not stack:
            result[0] = value
            return
        frame = stack[-1]
        if frame[0] == "a":
            frame[1].append(value)
        else:
            frame[1][frame[3]] = value
            frame[2] = "comma"

    stack: list[list] = []
    result: list[Any] = [None]

    skip_ws()
    if pos >= n:
        raise JsonDecodeError("空文档")

    while True:
        frame = stack[-1] if stack else None
        stack_done = False

        if frame is None:
            skip_ws()
            if not stack and result[0] is not None:
                if pos != n:
                    raise JsonDecodeError(
                        f"文档结束后仍有多余内容，位置 {pos}"
                    )
                return result[0]
            if pos >= n:
                raise JsonDecodeError("文档提前结束，缺少值")
            token = read_value()
            if token[0] == "s":
                assign(token[1])
                stack_done = True
            else:
                stack.append(token)
                stack_done = False

        elif frame[0] == "a":
            skip_ws()
            if pos >= n:
                raise JsonDecodeError("文档提前结束，数组未闭合")
            state = frame[2]  # start / comma / after_comma
            if state == "comma":
                if text[pos] == ",":
                    pos += 1
                    frame[2] = "after_comma"
                elif text[pos] == "]":
                    pos += 1
                    completed = stack.pop()[1]
                    assign(completed)
                    stack_done = not stack
                else:
                    raise JsonDecodeError(
                        f"数组缺少 ',' 或 ']'，位置 {pos}"
                    )
                stack_done = False
            elif state == "after_comma":
                # 逗号之后必须是值，不允许直接 ']'
                if text[pos] == "]":
                    raise JsonDecodeError(
                        f"数组逗号后缺少元素，位置 {pos}"
                    )
                token = read_value()
                if token[0] == "s":
                    assign(token[1])
                else:
                    stack.append(token)
                frame[2] = "comma"
                stack_done = False
            else:  # start：允许 ']'（空数组）或第一个元素
                if text[pos] == "]":
                    pos += 1
                    completed = stack.pop()[1]
                    assign(completed)
                    stack_done = not stack
                else:
                    token = read_value()
                    if token[0] == "s":
                        assign(token[1])
                    else:
                        stack.append(token)
                    frame[2] = "comma"
                    stack_done = False

        else:  # 对象帧
            state = frame[2]  # key/colon/value/comma/after_comma
            skip_ws()
            if pos >= n:
                raise JsonDecodeError("文档提前结束，对象未闭合")
            if state in ("key", "after_comma"):
                # after_comma 时逗号后必须还有键，不允许直接 '}'
                if text[pos] == "}":
                    if state == "after_comma":
                        raise JsonDecodeError(
                            f"对象逗号后缺少键，位置 {pos}"
                        )
                    pos += 1
                    completed = stack.pop()[1]
                    assign(completed)
                    stack_done = not stack
                elif text[pos] == '"':
                    frame[3] = parse_string()
                    frame[2] = "colon"
                    stack_done = False
                else:
                    raise JsonDecodeError(
                        f"对象键必须是字符串，位置 {pos}"
                    )
            elif state == "colon":
                if text[pos] != ":":
                    raise JsonDecodeError(f"对象缺少 ':'，位置 {pos}")
                pos += 1
                frame[2] = "value"
                stack_done = False
            elif state == "value":
                token = read_value()
                if token[0] == "s":
                    assign(token[1])
                else:
                    stack.append(token)
                stack_done = False
            else:  # comma
                if text[pos] == "}":
                    pos += 1
                    completed = stack.pop()[1]
                    assign(completed)
                    stack_done = not stack
                elif text[pos] == ",":
                    pos += 1
                    frame[2] = "after_comma"
                    stack_done = False
                else:
                    raise JsonDecodeError(
                        f"对象缺少 ',' 或 '}}'，位置 {pos}"
                    )

        if stack_done:
            skip_ws()
            if pos != n:
                raise JsonDecodeError(
                    f"文档结束后仍有多余内容，位置 {pos}"
                )
            return result[0]


def _hex4(text: str, pos: int) -> int:
    if pos + 4 > len(text):
        raise JsonDecodeError("\\u 转义不完整")
    try:
        return int(text[pos:pos + 4], 16)
    except ValueError:
        raise JsonDecodeError(f"非法 \\u 转义，位置 {pos}") from None


def load(fp) -> Any:
    return loads(fp.read())


# ================================================================ 序列化

def dumps(value: Any, ensure_ascii: bool = True, indent: int | None = None) -> str:
    """迭代式 JSON 序列化，支持本服务产出的全部类型。

    栈帧为 ``(kind, payload, depth)``：

    * ``v``  任意待输出值；dict/list 展开为括号 + 子元素帧；
    * ``s``  已编码的字面文本（键、标量、括号、冒号、逗号）；
    * ``nl`` 缩进换行（仅 indent 模式）。

    展开容器时为每个子元素压入 ``元素 + 前置分隔符``，元素逆序压栈，
    因此实际输出顺序为标准的 ``键: 值,\n  ...``。栈深度只与容器
    数量成正比，与嵌套深度无关。
    """
    out: list[str] = []
    stack: list[tuple[str, Any, int]] = [("v", value, 0)]

    while stack:
        kind, payload, depth = stack.pop()

        if kind == "nl":
            if indent is not None:
                out.append("\n" + (" " * (indent * depth)))
            continue

        if kind == "s":
            out.append(payload)
            continue

        # kind == "v"
        if isinstance(payload, dict):
            if not payload:
                out.append("{}")
                continue
            out.append("{")
            entries = list(payload.items())
            child_depth = depth + 1
            pretty = indent is not None
            stack.append(("s", "}", 0))
            stack.append(("nl", None, depth))
            for index, (key, val) in reversed(list(enumerate(entries))):
                # 期望输出：[分隔符][换行缩进]"key": value，故逆序压栈
                stack.append(("v", val, child_depth))
                stack.append(("s", ": ", 0))
                stack.append(("s", _encode_string(key, ensure_ascii), 0))
                stack.append(("nl", None, child_depth))
                if index > 0:
                    stack.append(
                        ("s", ",", 0) if pretty else ("s", ", ", 0)
                    )

        elif isinstance(payload, (list, tuple)):
            if not payload:
                out.append("[]")
                continue
            out.append("[")
            entries = list(payload)
            child_depth = depth + 1
            pretty = indent is not None
            stack.append(("s", "]", 0))
            stack.append(("nl", None, depth))
            for index, val in reversed(list(enumerate(entries))):
                # 期望输出顺序：[分隔符][换行缩进][值]，故逆序压栈
                stack.append(("v", val, child_depth))
                stack.append(("nl", None, child_depth))
                if index > 0:
                    stack.append(("s", "," if pretty else ", ", 0))

        else:
            out.append(_encode_scalar(payload, ensure_ascii))

    return "".join(out)


def dump(value: Any, fp, ensure_ascii: bool = True, indent: int | None = None) -> None:
    fp.write(dumps(value, ensure_ascii=ensure_ascii, indent=indent))


def _encode_scalar(v: Any, ensure_ascii: bool) -> str:
    if v is None:
        return "null"
    if v is True:  # bool 必须在 int 之前判断
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        return _encode_string(v, ensure_ascii)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    raise TypeError(f"不支持 JSON 序列化的类型: {type(v).__name__}")


_STR_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _encode_string(text: str, ensure_ascii: bool) -> str:
    parts = ['"']
    for ch in text:
        mapped = _STR_ESCAPES.get(ch)
        if mapped is not None:
            parts.append(mapped)
        elif ensure_ascii and ord(ch) > 0x7E:
            code = ord(ch)
            if code <= 0xFFFF:
                parts.append("\\u%04x" % code)
            else:
                code -= 0x10000
                high = 0xD800 + (code >> 10)
                low = 0xDC00 + (code & 0x3FF)
                parts.append("\\u%04x\\u%04x" % (high, low))
        elif ord(ch) < 0x20:
            parts.append("\\u%04x" % ord(ch))
        else:
            parts.append(ch)
    parts.append('"')
    return "".join(parts)
