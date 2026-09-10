"""ZIP 安全校验与受控解压。

拒绝以下压缩包：
* 含路径穿越条目 (``../``、绝对路径、盘符、反斜杠、保留名)；
* 条目数量超过限制；
* 解压后单条目 / 总体积超过限制；
* 压缩比超过限制 (ZIP 炸弹特征)；
* 同名条目重复；
* 非 ZIP / 加密 / 非普通文件条目。

校验依据 ZIP 中央目录声明的元数据，并在实际解压时再次以硬上限读取，
避免“声明体积小、实际炸弹”的绕过手法。
"""

from __future__ import annotations

import os
import posixpath
import re
import zipfile
from pathlib import Path

from . import config


class ZipRejected(Exception):
    """压缩包未通过安全校验。"""


# Windows 保留设备名 (CON / PRN / AUX / NUL / COM1..9 / LPT1..9)
_WIN_RESERVED = re.compile(
    r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)", re.IGNORECASE
)


def _safe_member_name(name: str) -> bool:
    """判断 ZIP 条目名是否安全（不穿越到目标目录之外）。"""
    if not name or name.strip() == "":
        return False
    # 反斜杠在不同解压工具下可能被当作分隔符，统一拒绝
    if "\\" in name:
        return False
    # 盘符 (C:) 与 Windows 绝对路径 (\foo)
    if re.match(r"^[a-zA-Z]:", name) or name.startswith("/"):
        return False
    # posixpath.normpath 规范化后逐段检查
    normalized = posixpath.normpath(name)
    if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
        return False
    parts = normalized.split("/")
    for part in parts:
        if part in ("", "..", "."):
            return False
        # 末尾的点/空格在 Windows 下会被吞掉，可能绕过前缀检查
        if part != part.rstrip(". "):
            return False
        if _WIN_RESERVED.match(part):
            return False
    return True


def inspect_zip(path: str | os.PathLike) -> list[zipfile.ZipInfo]:
    """仅做中央目录级校验，返回通过校验的条目列表（不读内容）。"""
    if not zipfile.is_zipfile(path):
        raise ZipRejected("不是合法的 ZIP 文件 (缺少 ZIP 签名/中央目录)")

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ZipRejected(f"ZIP 结构损坏: {exc}") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > config.MAX_ENTRIES:
            raise ZipRejected(
                f"条目数量 {len(infos)} 超过上限 {config.MAX_ENTRIES}"
            )

        seen: set[str] = set()
        total_uncompressed = 0
        for info in infos:
            # 仅处理普通文件：目录、符号链接、设备等一律拒绝，
            # 防止符号链接配合解压写出目标目录。
            mode = info.external_attr >> 16
            if stat_is_link_or_special(mode):
                raise ZipRejected(f"条目 {info.filename!r} 不是普通文件，已拒绝")
            if info.is_dir():
                raise ZipRejected(f"不允许目录条目: {info.filename!r}")
            if info.flag_bits & 0x1:
                raise ZipRejected(f"不支持加密条目: {info.filename!r}")
            if not _safe_member_name(info.filename):
                raise ZipRejected(f"检测到非法/路径穿越条目: {info.filename!r}")

            # 同名条目（不同大小写目录等）容易导致覆盖，拒绝
            key = info.filename.lower()
            if key in seen:
                raise ZipRejected(f"重复条目名: {info.filename!r}")
            seen.add(key)

            declared = info.file_size
            if declared < 0:
                raise ZipRejected(f"条目体积非法: {info.filename!r}")
            if declared > config.MAX_ENTRY_SIZE:
                raise ZipRejected(
                    f"条目 {info.filename!r} 解压后 "
                    f"{declared} 字节，超过单条目上限 "
                    f"{config.MAX_ENTRY_SIZE}"
                )

            total_uncompressed += declared
            if total_uncompressed > config.MAX_TOTAL_UNCOMPRESSED:
                raise ZipRejected(
                    f"解压总体积超过上限 {config.MAX_TOTAL_UNCOMPRESSED} 字节"
                )

            compressed = max(info.compress_size, 1)
            if declared // compressed > config.MAX_COMPRESSION_RATIO:
                raise ZipRejected(
                    f"条目 {info.filename!r} 压缩比 "
                    f"{declared / compressed:.1f}:1 超过上限 "
                    f"{config.MAX_COMPRESSION_RATIO}:1，疑似 ZIP 炸弹"
                )

        return infos


def stat_is_link_or_special(mode: int) -> bool:
    """根据 Unix mode 位判断是否为符号链接或特殊文件。

    ZIP 不强制带 Unix 类型位（Python ``writestr`` 默认只有权限位），
    因此：高 12 位类型字段为 0 时按普通文件放行；带类型位时，
    只要不是 regular/dir 就拒绝（symlink/fifo/device/socket）。
    """
    import stat

    if mode == 0:
        return False
    file_type = mode & 0o170000
    if file_type == 0:
        return False  # 只有权限位，无类型信息
    return not (
        stat.S_ISREG(mode) or stat.S_ISDIR(mode)
    )


def safe_extract(path: str | os.PathLike, dest: str | os.PathLike) -> list[Path]:
    """重新打开 ZIP 并把通过校验的条目解压到 ``dest``。

    实际读取时以硬上限截断，任何条目超出声明/上限都会拒绝整个包。
    返回解压出的文件路径列表（保持 ZIP 内相对结构）。
    """
    infos = inspect_zip(path)  # 解压前再校验一次，保证 TOCTOU 安全
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    with zipfile.ZipFile(path) as zf:
        for info in infos:
            target = (dest / info.filename).resolve()
            base = dest.resolve()
            if base not in target.parents and target != base:
                raise ZipRejected(
                    f"检测到路径穿越条目: {info.filename!r}"
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            remaining = min(
                config.MAX_ENTRY_SIZE,
                config.MAX_TOTAL_UNCOMPRESSED
                - sum(p.stat().st_size for p in written),
            )
            if remaining <= 0:
                raise ZipRejected("解压总体积超过上限")

            with zf.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if remaining < 0:
                        out.close()
                        target.unlink(missing_ok=True)
                        raise ZipRejected(
                            f"条目 {info.filename!r} 实际解压体积超限"
                        )
                    out.write(chunk)

            # 实际落盘体积与声明不符（声明小实际大）也拒绝
            if target.stat().st_size != info.file_size:
                target.unlink(missing_ok=True)
                raise ZipRejected(
                    f"条目 {info.filename!r} 实际体积与中央目录声明不一致"
                )
            written.append(target)

    return written
