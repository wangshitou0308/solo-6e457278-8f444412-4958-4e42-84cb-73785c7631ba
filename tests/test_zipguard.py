"""zipguard 安全校验测试：穿越 / 非普通文件 / 数量 / 体积 / 压缩比 / 损坏。"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401  必须先于 mailrecon 导入
from mailrecon import config, zipguard


@contextmanager
def limits(**overrides):
    """临时覆盖 mailrecon.config 中的上限常量。"""
    mapping = {
        "entries": "MAX_ENTRIES",
        "entry_size": "MAX_ENTRY_SIZE",
        "total_uncompressed": "MAX_TOTAL_UNCOMPRESSED",
        "compression_ratio": "MAX_COMPRESSION_RATIO",
    }
    attrs = {mapping[k]: v for k, v in overrides.items()}
    with mock.patch.multiple(config, **attrs):
        yield


def write_zip(members: dict[str, bytes], raw_external: dict[str, int] | None = None) -> str:
    path = tempfile.mktemp(suffix=".zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            if raw_external and name in raw_external:
                info = zipfile.ZipInfo(name)
                info.external_attr = raw_external[name]
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, data)
            else:
                zf.writestr(name, data)
    return path


class ZipGuardTest(unittest.TestCase):
    def test_normal_zip_passes(self):
        path = write_zip({"a.eml": b"From: a@b\r\n\r\nx", "nested/b.eml": b"y"})
        infos = zipguard.inspect_zip(path)
        self.assertEqual({i.filename for i in infos}, {"a.eml", "nested/b.eml"})
        with tempfile.TemporaryDirectory() as d:
            written = zipguard.safe_extract(path, d)
            self.assertEqual(len(written), 2)

    def test_path_traversal_dotdot_rejected(self):
        path = write_zip({"../../tmp/pwn.eml": b"x"})
        with self.assertRaises(zipguard.ZipRejected):
            zipguard.inspect_zip(path)

    def test_absolute_and_backslash_rejected(self):
        for name in ("/etc/passwd", "C:/Windows/x.eml", "a\\..\\b.eml"):
            with self.subTest(name=name):
                path = write_zip({name: b"x"})
                with self.assertRaises(zipguard.ZipRejected):
                    zipguard.inspect_zip(path)

    def test_symlink_entry_rejected(self):
        # S_IFLNK 0o120000
        path = write_zip(
            {"evil": b"/etc/passwd"},
            raw_external={"evil": 0o120777 << 16},
        )
        with self.assertRaises(zipguard.ZipRejected):
            zipguard.inspect_zip(path)

    def test_entry_count_limit(self):
        path = write_zip({f"{i}.eml": b"x" for i in range(11)})
        with limits(entries=10):
            with self.assertRaises(zipguard.ZipRejected):
                zipguard.inspect_zip(path)

    def test_total_size_limit(self):
        path = write_zip({"a.eml": b"x" * (3 * 1024)})
        with limits(total_uncompressed=2 * 1024):
            with self.assertRaises(zipguard.ZipRejected):
                zipguard.inspect_zip(path)

    def test_entry_size_limit(self):
        path = write_zip({"a.eml": b"x" * 500})
        with limits(entry_size=100):
            with self.assertRaises(zipguard.ZipRejected):
                zipguard.inspect_zip(path)

    def test_compression_ratio_bomb(self):
        # 高压缩比的零字节流
        payload = b"0" * 100_000
        path = write_zip({"a.eml": payload})
        with limits(compression_ratio=20):
            with self.assertRaises(zipguard.ZipRejected):
                zipguard.inspect_zip(path)

    def test_duplicate_name_rejected(self):
        buf = io.BytesIO()
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("a.eml", b"one")
                zf.writestr("a.eml", b"two")
        path = tempfile.mktemp(suffix=".zip")
        Path(path).write_bytes(buf.getvalue())
        with self.assertRaises(zipguard.ZipRejected):
            zipguard.inspect_zip(path)

    def test_not_a_zip(self):
        path = tempfile.mktemp(suffix=".zip")
        Path(path).write_bytes(b"PK\x03\x04not really a zip" * 5)
        with self.assertRaises(zipguard.ZipRejected):
            zipguard.inspect_zip(path)

    def test_encrypted_entry_rejected(self):
        # writestr 会重写 flag_bits，因此直接在 ZIP 字节里把通用标志
        # 位的 bit0（加密）置 1：本地文件头偏移 +6，中央目录头偏移 +8
        path = write_zip({"secret.eml": b"x"})
        data = bytearray(Path(path).read_bytes())
        local = data.find(b"PK\x03\x04")
        data[local + 6:local + 8] = b"\x01\x00"
        central = data.find(b"PK\x01\x02")
        data[central + 8:central + 10] = b"\x01\x00"
        Path(path).write_bytes(data)
        with self.assertRaises(zipguard.ZipRejected):
            zipguard.inspect_zip(path)

    def test_extract_stays_inside_dest(self):
        path = write_zip({"dir/sub/eml.eml": b"ok"})
        with tempfile.TemporaryDirectory() as d:
            zipguard.safe_extract(path, d)
            self.assertTrue((Path(d) / "dir/sub/eml.eml").is_file())
            # 解压目标之外没有写任何东西
            self.assertEqual(
                sorted(p.relative_to(d).as_posix() for p in Path(d).rglob("*") if p.is_file()),
                ["dir/sub/eml.eml"],
            )


if __name__ == "__main__":
    unittest.main()
