"""配置项：全部可通过环境变量覆盖，默认值面向单机本地使用。"""

from __future__ import annotations

import os
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是整数，实际为: {raw!r}") from None
    if value <= 0:
        raise ValueError(f"环境变量 {name} 必须是正整数，实际为: {value}")
    return value


def _data_dir() -> Path:
    raw = os.environ.get("MAILRECON_DATA_DIR")
    path = Path(raw) if raw else Path(__file__).resolve().parent.parent / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


# 服务监听地址 / 端口
HOST = os.environ.get("MAILRECON_HOST", "127.0.0.1")
PORT = _int_env("MAILRECON_PORT", 8080)

# 数据落盘根目录 (SQLite、原始 zip、结果 JSON 都在其下)
DATA_DIR = _data_dir()

# 上传体积上限 (HTTP 请求体字节数)
MAX_UPLOAD_BYTES = _int_env("MAILRECON_MAX_UPLOAD_BYTES", 500 * 1024 * 1024)
# 单个 ZIP 允许的条目数量上限
MAX_ENTRIES = _int_env("MAILRECON_MAX_ENTRIES", 10_000)
# ZIP 解压后总体积上限 (字节)
MAX_TOTAL_UNCOMPRESSED = _int_env(
    "MAILRECON_MAX_TOTAL_UNCOMPRESSED", 1024 * 1024 * 1024
)
# 单条目解压后体积上限 (字节)
MAX_ENTRY_SIZE = _int_env("MAILRECON_MAX_ENTRY_SIZE", 100 * 1024 * 1024)
# 压缩比上限 (解压体积 / 压缩体积)，超过视为 ZIP 炸弹
MAX_COMPRESSION_RATIO = _int_env(
    "MAILRECON_MAX_COMPRESSION_RATIO", 200
)
# 每个作业在结果 JSON 中保留的问题条数上限
MAX_ISSUES_REPORTED = _int_env("MAILRECON_MAX_ISSUES_REPORTED", 200)

# 作业状态
STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
