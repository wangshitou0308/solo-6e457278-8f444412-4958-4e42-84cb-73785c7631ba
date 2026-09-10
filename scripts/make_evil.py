#!/usr/bin/env python3
"""生成应被服务端拒绝的恶意/超限 ZIP，供手工验证安全策略。

用法:
    python scripts/make_evil.py traversal     # 含 ../../etc/passwd 风格条目
    python scripts/make_evil.py nonfile       # 含符号链接条目
    python scripts/make_evil.py toomany       # 条目数量超限
    python scripts/make_evil.py huge          # 解压总体积超限
    python scripts/make_evil.py bomb          # 压缩比超限 (ZIP 炸弹特征)
    python scripts/make_evil.py notzip        # 根本不是 ZIP

注意：超限类包会按“服务端默认上限”构造，体积在内存中生成，不会落盘炸弹；
若通过环境变量调大了上限，请相应调脚本参数。
"""

from __future__ import annotations

import io
import struct
import sys
import zipfile
from pathlib import Path

# 与 mailrecon.config 默认值保持一致（脚本不 import 包，保证可独立运行）
MAX_ENTRIES = 10_000
MAX_TOTAL = 1024 * 1024 * 1024
MAX_RATIO = 200

OUT_DIR = Path(__file__).resolve().parent.parent / "examples" / "evil"


def _save(name: str, build) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    buffer = io.BytesIO()
    build(zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED))
    path.write_bytes(buffer.getvalue())
    print(f"已生成 {path} ({path.stat().st_size} 字节)")
    return path


def make_traversal() -> Path:
    def build(zf: zipfile.ZipFile) -> None:
        zf.writestr("hello.eml", "From: a@b\r\n\r\nok")
        zf.writestr("../../../../tmp/pwned.eml", "evil")
    return _save("evil-traversal.zip", build)


def make_nonfile() -> Path:
    """手工拼一个带 Unix 符号链接 mode 位的 ZIP。"""
    def build(zf: zipfile.ZipFile) -> None:
        info = zipfile.ZipInfo("link-to-etc")
        # S_IFLNK = 0o120000，放到 external_attr 高 16 位
        info.external_attr = (0o120777 << 16) | 0x1FF
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, "/etc/passwd")
    return _save("evil-symlink.zip", build)


def make_toomany() -> Path:
    def build(zf: zipfile.ZipFile) -> None:
        for i in range(MAX_ENTRIES + 1):
            zf.writestr(f"mails/{i:06d}.eml", b"From: a@b\r\n\r\nx")
    return _save("evil-toomany.zip", build)


def make_huge() -> Path:
    """解压体积超过体积上限的随机数据条目。

    默认生成 2 MiB（随机数据不可压缩，不会同时触发压缩比规则）。
    配合服务端 ``MAILRECON_MAX_TOTAL_UNCOMPRESSED=1048576`` 即可看到拒绝；
    可用环境变量 EVIL_HUGE_KB 调整生成体积。
    """
    import os

    size = int(os.environ.get("EVIL_HUGE_KB", "2048")) * 1024

    def build(zf: zipfile.ZipFile) -> None:
        zf.writestr(
            "big.eml",
            os.urandom(size),
            compress_type=zipfile.ZIP_STORED,
        )
    return _save("evil-huge.zip", build)


def make_bomb() -> Path:
    """高压缩比但总体积不超上限的小炸弹。"""
    payload = b"0" * (MAX_RATIO * 4096 + 4096)  # 解压后约 804KB

    def build(zf: zipfile.ZipFile) -> None:
        zf.writestr("bomb.eml", payload, compress_type=zipfile.ZIP_DEFLATED)
    return _save("evil-bomb.zip", build)


def make_notzip() -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "evil-notzip.zip"
    path.write_bytes(b"PK\x03\x04" + b"this is not a real zip" * 10)
    print(f"已生成 {path} ({path.stat().st_size} 字节)")
    return path


KINDS = {
    "traversal": make_traversal,
    "nonfile": make_nonfile,
    "toomany": make_toomany,
    "huge": make_huge,
    "bomb": make_bomb,
    "notzip": make_notzip,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in KINDS:
        print(__doc__)
        return 2
    KINDS[argv[1]]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
