#!/usr/bin/env python3
"""生成案件合并示例包 examples/case-pack-1.zip 与 examples/case-pack-2.zip。

两个包模拟同一案件分两批导出的历史邮件，覆盖跨包合并的三类典型场景：

  1. 跨包补链：pack-1 中的催促邮件引用了缺失的父邮件，父邮件在 pack-2 中；
  2. 重复合并：同一份会议纪要在两个包中各出现一次（字节完全相同）；
  3. ID 冲突：同一 Message-ID 的周报在两个包中内容不同（并列保留）。

用法：python3 scripts/make_case_sample.py
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

BASE = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)

# 固定 Message-ID，保证重复运行生成同样的示例
ID_QUOTE = "<case-demo-quote@example.com>"
ID_COUNTER = "<case-demo-counter@example.com>"
ID_FINAL = "<case-demo-final@example.com>"
ID_CHASE = "<case-demo-chase@example.com>"
ID_MINUTES = "<case-demo-minutes@example.com>"
ID_WEEKLY = "<case-demo-weekly@example.com>"


def eml(
    msg_id: str,
    sender: tuple[str, str],
    to: list[tuple[str, str]],
    subject: str,
    body: str,
    date: datetime,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["Message-ID"] = msg_id
    msg["From"] = f"{sender[0]} <{sender[1]}>"
    msg["To"] = ", ".join(f"{n} <{a}>" for n, a in to)
    msg["Subject"] = subject
    msg["Date"] = format_datetime(date)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    msg.set_content(body)
    return bytes(msg)


def minutes_eml() -> bytes:
    """两个包中字节完全相同的会议纪要（用于演示 SHA-256 去重合并）。"""
    return eml(
        ID_MINUTES,
        ("法务部", "legal@example.com"),
        [("李雷", "lilei@example.com"), ("客户成功部", "cs@example.com")],
        "会议纪要：2026-08-21 合同条款对齐",
        "结论：违约条款上限按年度费用 100% 执行，付款账期 30 天。",
        BASE.replace(day=21),
    )


def build_pack1() -> dict[str, bytes]:
    sales = ("销售部", "sales@example.com")
    legal = ("法务部", "legal@example.com")
    return {
        "01-quote.eml": eml(
            ID_QUOTE, sales, [("客户", "client@external.test")],
            "2026 年度服务报价", "附件报价单请查收，有效期 30 天。", BASE,
        ),
        "02-counter.eml": eml(
            ID_COUNTER, ("客户", "client@external.test"), [sales],
            "Re: 2026 年度服务报价", "价格偏高，希望按九折执行。",
            BASE.replace(day=20, hour=14),
            in_reply_to=ID_QUOTE, references=[ID_QUOTE],
        ),
        # 引用的“最终版合同”邮件不在本包内（在 pack-2 中）——跨包补链场景
        "03-chase.eml": eml(
            ID_CHASE, legal, [("客户", "client@external.test")],
            "Re: 最终版合同确认", "请尽快确认最终版合同签署时间。",
            BASE.replace(day=25),
            in_reply_to=ID_FINAL, references=[ID_QUOTE, ID_FINAL],
        ),
        "shared/minutes.eml": minutes_eml(),
        # 与 pack-2 中同 ID 但内容不同的周报——ID 冲突场景
        "weekly/report.eml": eml(
            ID_WEEKLY, legal, [("管理层", "mgmt@example.com")],
            "合同审查周报（第 34 周）", "本周完成 3 份合同审查，无重大风险。",
            BASE.replace(day=22),
        ),
    }


def build_pack2() -> dict[str, bytes]:
    legal = ("法务部", "legal@example.com")
    return {
        # pack-1 中 03-chase.eml 缺失的父邮件
        "contract-final.eml": eml(
            ID_FINAL, ("客户", "client@external.test"), [legal],
            "最终版合同确认", "最终版合同已盖章扫描，请安排签署流程。",
            BASE.replace(day=24),
            in_reply_to=ID_COUNTER, references=[ID_QUOTE, ID_COUNTER],
        ),
        # 与 pack-1 字节完全相同的重复邮件
        "backup/minutes.eml": minutes_eml(),
        # 与 pack-1 同 Message-ID 但内容不同的周报（修订版）
        "weekly-report-revised.eml": eml(
            ID_WEEKLY, legal, [("管理层", "mgmt@example.com")],
            "合同审查周报（第 34 周·修订）",
            "本周完成 3 份合同审查；补充：1 份存在账期风险，已升级。",
            BASE.replace(day=22, hour=18),
        ),
    }


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
    write_zip(build_pack1(), out_dir / "case-pack-1.zip")
    write_zip(build_pack2(), out_dir / "case-pack-2.zip")


if __name__ == "__main__":
    main()
