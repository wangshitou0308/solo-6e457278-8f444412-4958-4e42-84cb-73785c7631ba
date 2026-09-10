"""测试公共环境：在导入 mailrecon 之前固定临时数据目录。

超限测试使用 mailrecon.config 模块里的 patch_limits() 临时缩小上限。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="mailrecon-test-"))
os.environ["MAILRECON_DATA_DIR"] = str(_TMP)
os.environ.setdefault("MAILRECON_MAX_UPLOAD_BYTES", str(64 * 1024 * 1024))

DATA_DIR = _TMP
