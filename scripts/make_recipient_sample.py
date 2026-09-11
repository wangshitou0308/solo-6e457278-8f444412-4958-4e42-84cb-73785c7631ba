#!/usr/bin/env python3
"""生成收件人流转分析示例包：

* ``examples/recipient-mails.zip`` — 单包，覆盖四类流转事件与全部待复核
  背景：新增地址、未继续列入收件人、To/Cc 角色变化、reply-all 遗漏；
  邮件列表代发（Sender 与 From 不同域）、畸形地址、父邮件缺失；
* ``examples/recipient-case-a.zip`` / ``examples/recipient-case-b.zip`` —
  父邮件在 A 包、回复在 B 包，建成两个作业后合并为案件再分析，即可看到
  跨包补链父子边上的收件人流转（疑似 reply-all 遗漏）。

所有邮件均为合成样本：分析只比较 From/To/Cc **可见头**，不读取 Bcc、
不使用 SMTP 信封信息，也不推断实际送达对象。

用法：python3 scripts/make_recipient_sample.py
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

# 固定 Message-ID，保证重复运行生成同样的示例
ID_ROOT = "<rf-root@example.com>"
ID_R2 = "<rf-reply-2@example.com>"
ID_R3 = "<rf-reply-3@example.com>"
ID_LIST = "<rf-list-post@list.example.org>"
ID_BAD = "<rf-bad-root@y.com>"
ID_CASE_ROOT = "<rf-case-root@partner-a.example>"


def eml(
    msg_id: str,
    sender: str,
    to: str,
    subject: str,
    date: str,
    body: str,
    *,
    cc: str | None = None,
    sender_header: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> bytes:
    """拼装原始邮件字节。"""
    lines = [f"Message-ID: {msg_id}", f"From: {sender}"]
    if sender_header:
        lines.append(f"Sender: {sender_header}")
    lines.append(f"To: {to}")
    if cc:
        lines.append(f"Cc: {cc}")
    lines.append(f"Subject: {subject}")
    lines.append(f"Date: {date}")
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if references:
        lines.append("References: " + " ".join(references))
    lines.append("")
    lines.append(body)
    return "\r\n".join(lines).encode("utf-8")


def build_pack() -> dict[str, bytes]:
    return {
        # ---- 线程 1：正常可见头的逐封比较 -------------------------------
        # 1) 父邮件：法务发 Bob、Carl，抄送 Dana
        "01-root.eml": eml(
            ID_ROOT,
            "法务 张 <legal@example.com>",
            "Bob <bob@example.org>, Carl <carl@example.org>",
            cc="Dana <dana@example.org>",
            subject="合同付款安排",
            date="Mon, 07 Sep 2026 09:00:00 +0000",
            body="各位好，请确认本周合同付款安排。",
        ),
        # 2) Bob 只回复发件人：Carl、Dana 未继续列入，构成 reply-all 遗漏
        "02-reply-only-sender.eml": eml(
            "<rf-reply-1@example.com>",
            "Bob <bob@example.org>",
            "法务 张 <legal@example.com>",
            subject="Re: 合同付款安排",
            date="Mon, 07 Sep 2026 09:20:00 +0000",
            body="我这边没问题。",
            in_reply_to=ID_ROOT,
            references=[ID_ROOT],
        ),
        # 3) 法务重新拉齐：新增 Erin（added），其余为上封的遗漏者回归
        "03-reply-with-newcomer.eml": eml(
            ID_R2,
            "法务 张 <legal@example.com>",
            "Bob <bob@example.org>, Erin <erin@example.org>",
            cc="Carl <carl@example.org>, Dana <dana@example.org>",
            subject="Re: 合同付款安排",
            date="Mon, 07 Sep 2026 10:00:00 +0000",
            body="Erin 加入跟进；Carl、Dana 请继续留意。",
            in_reply_to="<rf-reply-1@example.com>",
            references=[ID_ROOT, "<rf-reply-1@example.com>"],
        ),
        # 4) Bob 再回复：Erin 从 To 变 Cc（role_changed），
        #    Carl、Dana 被移出收件人（dropped + reply_all_omitted）
        "04-role-change-and-drop.eml": eml(
            ID_R3,
            "Bob <bob@example.org>",
            "法务 张 <legal@example.com>",
            cc="Erin <erin@example.org>",
            subject="Re: 合同付款安排",
            date="Mon, 07 Sep 2026 11:00:00 +0000",
            body="Erin 改为知会即可；Carl、Dana 这边不用再跟。",
            in_reply_to=ID_R2,
            references=[ID_ROOT, "<rf-reply-1@example.com>", ID_R2],
        ),
        # ---- 线程 2：邮件列表代发，差异只列为待复核 -----------------------
        # 5) 经列表代发：Sender 与 From 不同域（列表典型迹象）
        "05-list-post.eml": eml(
            ID_LIST,
            "Ann <ann@example.net>",
            "proj-owners@list.example.org",
            subject="[proj] 本周构建通知",
            date="Tue, 08 Sep 2026 09:00:00 +0000",
            body="本周构建由列表转发。",
            sender_header="list-manager@list.example.org",
        ),
        # 6) 列表参与者回复并带新人：因父邮件有列表迹象，差异不升为事件
        "06-list-reply.eml": eml(
            "<rf-list-reply@example.net>",
            "Team <team@example.net>",
            "Ann <ann@example.net>, New Person <newp@example.net>",
            subject="Re: [proj] 本周构建通知",
            date="Tue, 08 Sep 2026 09:40:00 +0000",
            body="收到，newp 也加入。",
            in_reply_to=ID_LIST,
            references=[ID_LIST],
        ),
        # ---- 线程 3：畸形地址，差异只列为待复核 ---------------------------
        # 7) To 中混入无法归一化的地址（无 @）
        "07-malformed-root.eml": eml(
            ID_BAD,
            "A <a@y.example>",
            "B <b@y.example>, finance-team",
            subject="对账单（地址异常样本）",
            date="Wed, 09 Sep 2026 09:00:00 +0000",
            body="本邮件含一个畸形收件人地址。",
        ),
        # 8) 回复带新地址：因父子任一侧有畸形地址，不生成客观事件
        "08-malformed-reply.eml": eml(
            "<rf-bad-reply@y.example>",
            "B <b@y.example>",
            "A <a@y.example>, Fresh <fresh@y.example>",
            subject="Re: 对账单（地址异常样本）",
            date="Wed, 09 Sep 2026 10:00:00 +0000",
            body="回复中新增 fresh，但差异只列为待复核。",
            in_reply_to=ID_BAD,
            references=[ID_BAD],
        ),
        # ---- 线程 4：父邮件缺失 -------------------------------------------
        # 9) 声称回复一封不在包内的邮件：无法比较，只列 missing_parent
        "09-orphan.eml": eml(
            "<rf-orphan@z.example>",
            "X <x@z.example>",
            "Y <y@z.example>",
            subject="Re: 一封未收录的邮件",
            date="Thu, 10 Sep 2026 09:00:00 +0000",
            body="父邮件不在目标范围内。",
            in_reply_to="<rf-ghost@z.example>",
            references=["<rf-ghost@z.example>"],
        ),
    }


def build_case_packs() -> tuple[dict[str, bytes], dict[str, bytes]]:
    """父邮件在 A 包、回复在 B 包；合并案件后跨包补链再分析。

    回复只写了父邮件发件人，父邮件的另外两位收件人构成跨包边上的
    reply-all 遗漏。
    """
    pack_a = {
        "case-a-root.eml": eml(
            ID_CASE_ROOT,
            "Alice <alice@partner-a.example>",
            "Bob <bob@partner-b.example>, Carol <carol@partner-b.example>",
            cc="David <david@partner-c.example>",
            subject="跨包合同评审",
            date="Fri, 11 Sep 2026 09:00:00 +0000",
            body="A 包导出：请 Bob、Carol 评审，David 知会。",
        ),
    }
    pack_b = {
        "case-b-reply.eml": eml(
            "<rf-case-reply@partner-b.example>",
            "Bob <bob@partner-b.example>",
            "Alice <alice@partner-a.example>",
            subject="Re: 跨包合同评审",
            date="Fri, 11 Sep 2026 10:30:00 +0000",
            body=(
                "B 包导出：只回复了 Alice；Carol、David 未继续列入，"
                "案件级分析将在跨包补链的父子边上标出 reply-all 遗漏。"
            ),
            in_reply_to=ID_CASE_ROOT,
            references=[ID_CASE_ROOT],
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
    write_zip(build_pack(), out_dir / "recipient-mails.zip")
    pack_a, pack_b = build_case_packs()
    write_zip(pack_a, out_dir / "recipient-case-a.zip")
    write_zip(pack_b, out_dir / "recipient-case-b.zip")


if __name__ == "__main__":
    main()
