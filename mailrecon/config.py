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


def _float_env(name: str, default: float, *, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是数字，实际为: {raw!r}") from None
    if not 0 < value <= maximum:
        raise ValueError(
            f"环境变量 {name} 必须在 (0, {maximum}] 区间内，实际为: {value}"
        )
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

# 单个案件允许合并的源作业数量上限
MAX_CASE_JOBS = _int_env("MAILRECON_MAX_CASE_JOBS", 100)
# 单个案件允许合并的邮件总量上限（各源作业 email_count 之和）
MAX_CASE_EMAILS = _int_env("MAILRECON_MAX_CASE_EMAILS", 100_000)

# 时序核验默认阈值：客户端时钟偏差容差（秒）
DEFAULT_CLOCK_SKEW_SECONDS = _int_env(
    "MAILRECON_DEFAULT_CLOCK_SKEW_SECONDS", 120
)
# 时序核验默认阈值：单跳传输耗时上限（秒）
DEFAULT_MAX_TRANSIT_SECONDS = _int_env(
    "MAILRECON_DEFAULT_MAX_TRANSIT_SECONDS", 300
)
# 用户可配置阈值的上限（秒），防止误填超大值导致核验失效
MAX_THRESHOLD_SECONDS = _int_env(
    "MAILRECON_MAX_THRESHOLD_SECONDS", 7 * 24 * 3600
)

# 引文溯源：疑似改写的相似度下限（0, 1]，低于该值只列“相似度不足”待复核
QUOTE_PARAPHRASE_MIN = _float_env(
    "MAILRECON_QUOTE_PARAPHRASE_MIN", 0.6, maximum=1.0
)
# 引文溯源：每段引文保留的候选来源邮件数上限（并列候选，不自动归属）
QUOTE_MAX_CANDIDATES = _int_env("MAILRECON_QUOTE_MAX_CANDIDATES", 20)
# 引文溯源：参与疑似改写判定的文本长度上限（超出只做精确/截取匹配）
QUOTE_PARAPHRASE_MAX_CHARS = _int_env(
    "MAILRECON_QUOTE_PARAPHRASE_MAX_CHARS", 20000
)

# 作业状态
STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
