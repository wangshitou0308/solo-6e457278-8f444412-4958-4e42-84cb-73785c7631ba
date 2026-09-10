#!/usr/bin/env python3
"""生成时序核验示例包：

* ``examples/timing-mails.zip`` — 单包，覆盖时序核验的典型场景：
  正常多跳、客户端时钟偏差、异常传输耗时、相邻跳逆序、回复早于父邮件、
  Received 缺时区（不猜测，只标注）；
* ``examples/timing-chain-a.zip`` / ``examples/timing-chain-b.zip`` —
  同一 Message-ID 在两个来源中 Received 传输链不同，用于案件级
  “传输链不一致并列展示”演示（两个包各自建成作业后合并为案件再分析）。

用法：python3 scripts/make_timing_sample.py
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

# 固定 Message-ID，保证重复运行生成同样的示例
ID_CONTRACT = "<timing-contract@example.com>"
ID_SKew = "<timing-skew@example.com>"
ID_SLOW = "<timing-slow@example.com>"
ID_INVERT = "<timing-invert@example.com>"
ID_NOTZ = "<timing-notz@example.com>"
ID_EARLY = "<timing-early@example.com>"
ID_REPORT = "<timing-report@example.com>"


def eml(
    msg_id: str,
    subject: str,
    date: str,
    body: str,
    hops: list[str] | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> bytes:
    """拼装原始邮件字节。

    ``hops`` 按**传输路径方向**（先发生的在前）给出 Received 头值；
    写文件时逆序放置（最后一跳在最上方），与真实服务器前置行为一致。
    """
    lines: list[str] = []
    for hop_value in reversed(hops or []):
        lines.append(f"Received: {hop_value}")
    lines.append(f"Message-ID: {msg_id}")
    lines.append("From: 李雷 <lilei@example.com>")
    lines.append("To: 法务部 <legal@example.com>")
    lines.append(f"Subject: {subject}")
    lines.append(f"Date: {date}")
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if references:
        lines.append("References: " + " ".join(references))
    lines.append("")
    lines.append(body)
    return "\r\n".join(lines).encode("utf-8")


def build_timing_pack() -> dict[str, bytes]:
    return {
        # 正常两跳：Date 与首跳差 8 秒，跳间 25 秒——不应产生任何结论
        "01-normal.eml": eml(
            ID_CONTRACT, "合同签署时间确认",
            "Mon, 01 Sep 2026 09:00:00 +0000",
            "请确认合同签署时间是否为本周五。",
            hops=[
                "from client-lilei ([192.0.2.10]) by mail.example.com "
                "with ESMTP; Mon, 01 Sep 2026 09:00:08 +0000",
                "from mail.example.com by mx.example.org "
                "with ESMTPS; Mon, 01 Sep 2026 09:00:33 +0000",
            ],
        ),
        # 客户端时钟偏差：Date 09:00，首跳服务器 09:12 才收到（差 720 秒）
        "02-clock-skew.eml": eml(
            ID_SKew, "补充材料（发送端时钟异常）",
            "Mon, 01 Sep 2026 09:00:00 +0000",
            "补充材料请查收。",
            hops=[
                "from client-han ([192.0.2.20]) by mail.example.com "
                "with ESMTP; Mon, 01 Sep 2026 09:12:00 +0000",
            ],
        ),
        # 异常传输耗时：两跳间隔 45 分钟
        "03-slow-transit.eml": eml(
            ID_SLOW, "延迟到达的通知",
            "Mon, 01 Sep 2026 09:00:00 +0000",
            "这封邮件在中继上滞留了很久。",
            hops=[
                "from client-wang by relay.example.com "
                "with ESMTP; Mon, 01 Sep 2026 09:00:30 +0000",
                "from relay.example.com by mx.example.org "
                "with ESMTPS; Mon, 01 Sep 2026 09:45:30 +0000",
            ],
        ),
        # 相邻跳逆序：后一跳时间反而更早（不猜测原因，并列证据）
        "04-hop-inversion.eml": eml(
            ID_INVERT, "跳点时间逆序示例",
            "Mon, 01 Sep 2026 09:00:00 +0000",
            "第二跳的时间戳早于第一跳。",
            hops=[
                "from client-zhao by relay-a.example.com "
                "with ESMTP; Mon, 01 Sep 2026 09:30:00 +0000",
                "from relay-a.example.com by mx.example.org "
                "with ESMTPS; Mon, 01 Sep 2026 09:05:00 +0000",
            ],
        ),
        # Received 缺时区：不换算、不猜测，只标注
        "05-missing-timezone.eml": eml(
            ID_NOTZ, "缺时区的传输记录",
            "Mon, 01 Sep 2026 09:00:00 +0000",
            "内部中继没有写时区。",
            hops=[
                "from client-sun by mail.example.com "
                "with ESMTP; Mon, 01 Sep 2026 09:00:10 +0000",
                "by internal-relay.local with LMTP; "
                "Mon, 01 Sep 2026 09:00:20",
            ],
        ),
        # 回复早于父邮件 2 小时（父邮件为 01-normal）
        "06-early-reply.eml": eml(
            ID_EARLY, "Re: 合同签署时间确认",
            "Mon, 01 Sep 2026 07:00:00 +0000",
            "这封回复的 Date 早于父邮件。",
            in_reply_to=ID_CONTRACT, references=[ID_CONTRACT],
        ),
    }


def build_chain_packs() -> tuple[dict[str, bytes], dict[str, bytes]]:
    """同一 Message-ID 的进展周报，两个来源的 Received 链不同。"""
    pack_a = {
        "report.eml": eml(
            ID_REPORT, "项目进展周报",
            "Mon, 01 Sep 2026 09:00:00 +0000",
            "本周进展正常。",
            hops=[
                "from client-lilei by relay-one.example.com "
                "with ESMTP; Mon, 01 Sep 2026 09:00:05 +0000",
                "from relay-one.example.com by mx.example.org "
                "with ESMTPS; Mon, 01 Sep 2026 09:00:40 +0000",
            ],
        ),
    }
    pack_b = {
        "report-copy.eml": eml(
            ID_REPORT, "项目进展周报",
            "Mon, 01 Sep 2026 09:00:00 +0000",
            "本周进展正常。",
            hops=[
                "from client-lilei by relay-two.example.net "
                "with ESMTP; Mon, 01 Sep 2026 09:02:05 +0000",
            ],
        ),
    }
    return pack_a, pack_b


def write_zip(members: dict[str, bytes], out: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    out.write_bytes(buffer.getvalue())
    print(f"已生成 {out} ({len(members)} 个条目)")


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "examples"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_zip(build_timing_pack(), out_dir / "timing-mails.zip")
    pack_a, pack_b = build_chain_packs()
    write_zip(pack_a, out_dir / "timing-chain-a.zip")
    write_zip(pack_b, out_dir / "timing-chain-b.zip")


if __name__ == "__main__":
    main()
