#!/usr/bin/env python3
"""生成示例邮件包 examples/sample-mails.zip。

覆盖场景：
  1. 正常会话线程（3 封，References 链）
  2. 父邮件缺失的邮件
  3. 引用成环（A<->B）
  4. 同一 Message-ID 内容不同（ID 冲突）
  5. 缺少 Message-ID 的邮件
  6. 声明字符集无法解码（降级处理 + 问题标注）
  7. 含两个附件的邮件（只导出元数据与 SHA-256）
  8. HTML 正文邮件
  9. 非 .eml 文件（跳过并记录）
"""

from __future__ import annotations

import io
import zipfile
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from datetime import datetime, timezone
from pathlib import Path


def eml(
    msg_id: str | None,
    sender: tuple[str, str],
    to: list[tuple[str, str]],
    subject: str,
    body: str,
    date: datetime,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    if msg_id is not None:
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
    return msg


def attach(msg: EmailMessage, filename: str, data: bytes, ctype: str) -> None:
    maintype, subtype = ctype.split("/", 1)
    msg.add_attachment(
        data, maintype=maintype, subtype=subtype, filename=filename
    )


def build() -> dict[str, str | bytes]:
    members: dict[str, str | bytes] = {}

    base = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)

    # --- 线程 1：合同审查 (正常链)
    id_a = make_msgid("contract", "example.com")
    id_b = make_msgid("contract", "example.com")
    id_c = make_msgid("contract", "example.com")

    m = eml(
        id_a, ("李雷", "lilei@example.com"),
        [("法务部", "legal@example.com")],
        "【合同】2026 年度框架服务协议审查",
        "各位好，\n\n附件为客户回传的框架服务协议，请协助审查违约条款。\n\n李雷",
        base,
    )
    attach(m, "框架服务协议-节选.txt", "第六条 违约责任：……（节选文本）".encode("utf-8"),
           "text/plain")
    members["thread-contract/01-request.eml"] = bytes(m)

    m = eml(
        id_b, ("韩梅梅", "hanmeimei@example.com"),
        [("李雷", "lilei@example.com"), ("法务部", "legal@example.com")],
        "Re: 【合同】2026 年度框架服务协议审查",
        "李雷你好，\n\n第 6.2 条的赔偿上限建议不超过年度费用的 100%。\n\n韩梅梅",
        base.replace(day=2), in_reply_to=id_a, references=[id_a],
    )
    members["thread-contract/02-legal-reply.eml"] = bytes(m)

    m = eml(
        id_c, ("李雷", "lilei@example.com"),
        [("韩梅梅", "hanmeimei@example.com")],
        "Re: 【合同】2026 年度框架服务协议审查",
        "收到，已与客户沟通按 100% 上限修订，新版今日内回传。",
        base.replace(day=2, hour=15),
        in_reply_to=id_b, references=[id_a, id_b],
    )
    attach(m, "revision-notes.md", b"# revision notes\n- cap 100%\n",
           "text/markdown")
    members["thread-contract/03-update.eml"] = bytes(m)

    # --- 线程 2：父邮件缺失
    orphan_id = make_msgid("ticket", "support.example")
    m = eml(
        orphan_id, ("客户甲", "customer-a@external.test"),
        [("支持团队", "support@example.com")],
        "Re: 工单 #4421 登录异常",
        "问题仍然存在，附件是最新日志（注：上一封工单邮件不在本包内）。",
        base.replace(day=3),
        in_reply_to="<missing-ticket-4421@support.example>",
        references=["<missing-ticket-4421@support.example>"],
    )
    members["thread-orphan/orphan-reply.eml"] = bytes(m)

    # --- 线程 3：引用成环
    cyc_x = "<cycle-x@example.com>"
    cyc_y = "<cycle-y@example.com>"
    m = eml(
        cyc_x, ("王五", "wangwu@example.com"),
        [("赵六", "zhaoliu@example.com")],
        "循环引用测试 A",
        "这封邮件声称回复 B，B 又声称回复 A。",
        base.replace(day=4),
        in_reply_to=cyc_y, references=[cyc_y],
    )
    members["thread-cycle/cycle-a.eml"] = bytes(m)

    m = eml(
        cyc_y, ("赵六", "zhaoliu@example.com"),
        [("王五", "wangwu@example.com")],
        "循环引用测试 B",
        "闭环：本邮件回复 A。",
        base.replace(day=4, hour=1),
        in_reply_to=cyc_x, references=[cyc_x, cyc_y, cyc_x],
    )
    members["thread-cycle/cycle-b.eml"] = bytes(m)

    # --- 线程 4：重复 Message-ID 且内容不同
    dup = "<dup-id@example.com>"
    members["dup/version-1.eml"] = bytes(
        eml(
            dup, ("发件人", "sender@example.com"),
            [("收件人", "rcpt@example.com")],
            "同一 ID 的第一版", "这是第一版内容。",
            base.replace(day=5),
        )
    )
    members["dup/version-2.eml"] = bytes(
        eml(
            dup, ("发件人", "sender@example.com"),
            [("收件人", "rcpt@example.com")],
            "同一 ID 的第二版", "这是不同内容的第二版。",
            base.replace(day=5, hour=2),
        )
    )

    # --- 线程 5：缺少 Message-ID
    m = eml(
        None, ("匿名转发", "fwd@example.com"),
        [("法务部", "legal@example.com")],
        "转发：无 Message-ID 的历史邮件",
        "老系统导出的邮件缺少 Message-ID 头。",
        base.replace(day=6),
    )
    members["no-msgid.eml"] = bytes(m)

    # --- 线程 6：坏字符集（声明 x-fake-charset，实际内容是 UTF-8）
    import base64

    body_b64 = base64.encodebytes(
        "正文是 UTF-8，但字符集声明是伪造的：解析时应降级到 utf-8。".encode("utf-8")
    )
    raw = (
        b"Message-ID: <bad-charset@example.com>\r\n"
        b"From: =?utf-8?b?6IyD5Zu056ys5LqM?= <bad@example.com>\r\n"
        b"To: legal@example.com\r\n"
        b"Subject: =?utf-8?b?5rC45LmF5a2X56ym5Liy5pa55qGI?=\r\n"
        b"Date: Mon, 07 Sep 2026 10:00:00 +0800\r\n"
        b"Content-Type: text/plain; charset=x-fake-charset\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        + body_b64
    )
    members["bad-charset.eml"] = raw

    # --- 线程 7：HTML 正文
    m = EmailMessage()
    m["Message-ID"] = make_msgid("html", "example.com")
    m["From"] = "support@example.com"
    m["To"] = "customer-b@external.test"
    m["Subject"] = "工单 #4430 处理说明（HTML）"
    m["Date"] = format_datetime(base.replace(day=8))
    m.add_alternative(
        "<html><body><h2>处理说明</h2><p>请按<b>以下步骤</b>操作：</p>"
        "<ol><li>清除缓存</li><li>重新登录</li></ol>"
        "<script>alert('x')</script></body></html>",
        subtype="html",
    )
    members["html-only.eml"] = bytes(m)

    # --- 非 eml 文件：应被跳过记录
    members["README.txt"] = "这个文件不是邮件，处理时会出现在 skipped_entries 中。"

    return members


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "examples" / "sample-mails.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    members = build()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    out.write_bytes(buffer.getvalue())
    print(f"已生成 {out} ({len(members)} 个条目)")


if __name__ == "__main__":
    main()
