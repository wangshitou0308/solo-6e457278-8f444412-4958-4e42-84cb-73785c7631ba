#!/usr/bin/env python3
"""生成邮件声明身份核验示例包：

* ``examples/identity-mails.zip`` — 单包，覆盖四类发现与待复核背景：
  From 与 Sender/Return-Path 域不一致、回复链身份变化（显示名相同换域）、
  Message-ID 标识域漂移、DKIM h= 未覆盖 From；另含转发（Fwd:）、
  邮件列表迹象、字段缺失（无法核验，不判伪造）等待复核场景；
* ``examples/identity-list-a.zip`` / ``examples/identity-list-b.zip`` —
  同一发件人在两个来源中 Message-ID 标识域不同（会话级域漂移），
  建成两个作业后合并为案件再核验即可看到会话级
  ``message_id_domain_drift``。

所有邮件均为合成样本：**不查 DNS、不验证 DKIM 签名真伪**，DKIM 的
``b=`` 为占位值，仅用于演示 h= 覆盖关系解析。

用法：python3 scripts/make_identity_sample.py
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

# 固定 Message-ID，保证重复运行生成同样的示例
ID_NORMAL = "<identity-normal@example.com>"
ID_REPLY1 = "<identity-reply-1@example.com>"
ID_REPLY2 = "<identity-reply-2@example.com>"
ID_BANK = "<payment-notice@mail-forward.example>"
ID_DRIFT = "<thread-001@example.com>"


def eml(
    msg_id: str | None,
    sender: str,
    to: str,
    subject: str,
    date: str,
    body: str,
    *,
    sender_header: str | None = None,
    reply_to: str | None = None,
    return_path: str | None = None,
    dkim: list[str] | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    extra_headers: list[tuple[str, str]] | None = None,
) -> bytes:
    """拼装原始邮件字节（DKIM b= 为占位，不代表真实签名）。"""
    lines: list[str] = []
    if msg_id is not None:
        lines.append(f"Message-ID: {msg_id}")
    lines.append(f"From: {sender}")
    if sender_header:
        lines.append(f"Sender: {sender_header}")
    if reply_to:
        lines.append(f"Reply-To: {reply_to}")
    if return_path:
        lines.append(f"Return-Path: {return_path}")
    lines.append(f"To: {to}")
    lines.append(f"Subject: {subject}")
    lines.append(f"Date: {date}")
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if references:
        lines.append("References: " + " ".join(references))
    for sig in dkim or []:
        lines.append(f"DKIM-Signature: {sig}")
    for name, value in extra_headers or []:
        lines.append(f"{name}: {value}")
    lines.append("")
    lines.append(body)
    return "\r\n".join(lines).encode("utf-8")


def _dkim(domain: str, selector: str, h: str, *, identity: str | None = None) -> str:
    i_part = f"; i={identity}" if identity else ""
    return (
        f"v=1; a=rsa-sha256; d={domain}; s={selector}{i_part}; "
        f"h={h}; bh=PLACEHOLDER; b=PLACEHOLDERSIGNATURE"
    )


def build_identity_pack() -> dict[str, bytes]:
    return {
        # 1) 正常基线：From/Sender/Return-Path 同域，Message-ID 同域，
        #    DKIM h= 覆盖 From——不应产生任何发现
        "01-normal.eml": eml(
            ID_NORMAL,
            "李雷 <lilei@example.com>",
            "法务部 <legal@example.org>",
            "【对账】2026 年 8 月对账单",
            "Mon, 01 Sep 2026 09:00:00 +0000",
            "各位好，附件为 8 月对账单。",
            sender_header="李雷 <lilei@example.com>",
            return_path="<lilei@example.com>",
            dkim=[_dkim("example.com", "mail", "from:to:subject:date")],
        ),
        # 2) From 与 Sender/Return-Path 域不一致（代发平台），
        #    且 DKIM h= 未覆盖 From
        "02-sender-mismatch.eml": eml(
            "<newsletter-2026-09@example.com>",
            "市场部 <marketing@example.com>",
            "客户 <customer@example.org>",
            "9 月产品动态",
            "Mon, 01 Sep 2026 09:30:00 +0000",
            "本月产品更新如下。",
            sender_header="newsletter-bot@bulk-mailer.net",
            return_path="<bounces@bulk-mailer.net>",
            dkim=[_dkim("bulk-mailer.net", "s1", "to:subject:date")],
        ),
        # 3) DKIM 多重签名：列表签名未覆盖 From，但原始域签名覆盖了
        #    From——结论状态为 needs_review（另有签名覆盖）
        "03-list-resign.eml": eml(
            "<announce-42@example.com>",
            "公告 <announce@example.com>",
            "members@list.example.org",
            "邮件列表转发的公告",
            "Mon, 01 Sep 2026 10:00:00 +0000",
            "本公告经邮件列表转发。",
            sender_header="list-manager@list.example.org",
            reply_to="list-manager@list.example.org",
            return_path="<list-manager@list.example.org>",
            dkim=[
                _dkim("list.example.org", "list", "to:subject:date"),
                _dkim("example.com", "mail", "from:to:subject:date"),
            ],
        ),
        # 4) 回复链：第 1、2 封均为 lilei@example.com，第 3 封显示名相同
        #    但域变成 lookalike 域 examp1e.com（数字 1 仿 l）——
        #    reply_identity_change（observed），同时 DKIM h= 漏 From
        "04-reply-chain-1.eml": eml(
            ID_REPLY1,
            "李雷 <lilei@example.com>",
            "法务部 <legal@example.org>",
            "Re: 合同付款账户确认",
            "Tue, 02 Sep 2026 09:00:00 +0000",
            "账户信息见附件，请核对。",
            in_reply_to=ID_NORMAL,
            references=[ID_NORMAL],
            dkim=[_dkim("example.com", "mail", "from:to:subject:date")],
        ),
        "05-reply-chain-2.eml": eml(
            ID_REPLY2,
            "李雷 <lilei@example.com>",
            "法务部 <legal@example.org>",
            "Re: 合同付款账户确认",
            "Tue, 02 Sep 2026 10:00:00 +0000",
            "请以本邮件中的账户为准。",
            in_reply_to=ID_REPLY1,
            references=[ID_NORMAL, ID_REPLY1],
            dkim=[_dkim("example.com", "mail", "from:to:subject:date")],
        ),
        "06-reply-chain-impersonated.eml": eml(
            "<reply-pay-99@examp1e.com>",
            "李雷 <lilei@examp1e.com>",
            "法务部 <legal@example.org>",
            "Re: 合同付款账户确认",
            "Tue, 02 Sep 2026 11:00:00 +0000",
            "账户变更，请付款至新账户（演示样本，请勿模仿真实操作）。",
            in_reply_to=ID_REPLY2,
            references=[ID_NORMAL, ID_REPLY1, ID_REPLY2],
            dkim=[_dkim("examp1e.com", "k1", "to:subject:date")],
        ),
        # 7) 转发 + 邮件列表：From 为银行、Sender/Return-Path 为列表域，
        #    Message-ID 域为转发服务域——全部标 needs_review 待人工复核，
        #    不直接判定伪造
        "07-forwarded-bank.eml": eml(
            ID_BANK,
            "银行通知 <service@bigbank.example>",
            "李雷 <lilei@example.com>",
            "Fwd: 付款到账通知",
            "Wed, 03 Sep 2026 09:00:00 +0000",
            "---------- 转发邮件 ----------\n付款已到账。",
            sender_header="forward-bot@mail-forward.example",
            reply_to="it-helpdesk@mail-forward.example",
            return_path="<forward-bot@mail-forward.example>",
            dkim=[_dkim("mail-forward.example", "fw", "from:to:subject:date")],
        ),
        # 8) 字段缺失：无 From、无 Message-ID、无 DKIM——
        #    各项检查为 inconclusive（无法核验），只记待复核证据
        "08-missing-fields.eml": eml(
            None,
            "anonymous@unknown.invalid",
            "法务部 <legal@example.org>",
            "匿名材料",
            "Wed, 03 Sep 2026 14:00:00 +0000",
            "（本邮件缺少多个身份头）",
        ),
        # 9) DKIM 标签畸形：h= 标签缺失（无法判断覆盖关系），
        #    i= 身份域与 d= 不一致——解析异常全部留痕
        "09-dkim-malformed.eml": eml(
            "<malformed-dkim@example.com>",
            "测试 <test@example.com>",
            "法务部 <legal@example.org>",
            "DKIM 标签异常样本",
            "Thu, 04 Sep 2026 09:00:00 +0000",
            "h 标签缺失、i 与 d 不同域。",
            dkim=["v=1; d=example.com; s=broken; i=attacker@evil.test; b=xx"],
        ),
    }


def build_list_packs() -> tuple[dict[str, bytes], dict[str, bytes]]:
    """同一发件人同一回复线程，两个来源的 Message-ID 标识域不同。

    pack A 含线程首封（旧邮件系统导出，标识域 example.com），pack B 含
    其回复（迁移到新系统后的归档，标识域变为 example-corp.example）。
    两个包合并为案件后：回复经跨包补链挂到首封之下，会话级
    ``message_id_domain_drift`` 把同一发件人跨来源的两个标识域并列展示。
    """
    pack_a = {
        "thread-a.eml": eml(
            ID_DRIFT,
            "韩梅梅 <hanmeimei@example.com>",
            "法务部 <legal@example.org>",
            "周报第 12 期（A 来源导出）",
            "Fri, 05 Sep 2026 09:00:00 +0000",
            "本周工作汇总（来自旧邮件系统归档）。",
            dkim=[_dkim("example.com", "mail", "from:to:subject:date")],
        ),
    }
    pack_b = {
        "thread-b.eml": eml(
            "<thread-002@example-corp.example>",
            "韩梅梅 <hanmeimei@example.com>",
            "法务部 <legal@example.org>",
            "Re: 周报第 12 期（B 来源导出）",
            "Fri, 05 Sep 2026 09:05:00 +0000",
            "补充：迁移到新邮件系统后的归档，标识域随之变化。",
            dkim=[_dkim("example-corp.example", "mail", "from:to:subject:date")],
            in_reply_to=ID_DRIFT,
            references=[ID_DRIFT],
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
    write_zip(build_identity_pack(), out_dir / "identity-mails.zip")
    pack_a, pack_b = build_list_packs()
    write_zip(pack_a, out_dir / "identity-list-a.zip")
    write_zip(pack_b, out_dir / "identity-list-b.zip")


if __name__ == "__main__":
    main()
