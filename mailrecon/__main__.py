"""命令行入口：``python -m mailrecon``。"""

from __future__ import annotations

import argparse
import sys

from . import config
from .server import ApiServer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mailrecon",
        description="本地历史邮件包会话重建 API (仅标准库)",
    )
    parser.add_argument("--host", default=config.HOST, help="监听地址")
    parser.add_argument("--port", type=int, default=config.PORT, help="监听端口")
    args = parser.parse_args(argv)

    server = ApiServer(args.host, args.port)
    print(
        f"mailrecon 监听 http://{args.host}:{args.port}\n"
        f"数据目录: {config.DATA_DIR}\n"
        f"上限: 上传 {config.MAX_UPLOAD_BYTES} 字节 / "
        f"条目 {config.MAX_ENTRIES} / "
        f"解压总 {config.MAX_TOTAL_UNCOMPRESSED} 字节 / "
        f"压缩比 {config.MAX_COMPRESSION_RATIO}:1",
        file=sys.stderr,
    )
    try:
        server.serve()
    except KeyboardInterrupt:
        print("\n已停止", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
