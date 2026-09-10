"""mailparser 测试：头解析、字符集降级、附件元数据、坏邮件不抛异常。"""

from __future__ import annotations

import base64
import hashlib
import unittest
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone

import tests.support  # noqa: F401
from mailrecon import mailparser


def make_eml(
    msg_id="<a@x>",
    irt=None,
    refs=None,
    subject="测试主题",
    body="正文内容",
    sender=("张三", "zhang@x"),
    to=None,
    date=None,
):
    msg = EmailMessage()
    if msg_id:
        msg["Message-ID"] = msg_id
    msg["From"] = f"{sender[0]} <{sender[1]}>"
    msg["To"] = ", ".join(f"{n} <{a}>" for n, a in (to or [("李四", "li@x")]))
    msg["Subject"] = subject
    msg["Date"] = format_datetime(date or datetime(2026, 9, 1, tzinfo=timezone.utc))
    if irt:
        msg["In-Reply-To"] = irt
    if refs:
        msg["References"] = " ".join(refs)
    msg.set_content(body)
    return bytes(msg)


class MailParserTest(unittest.TestCase):
    def test_basic_headers(self):
        rec = mailparser.parse_eml(make_eml(), "a.eml")
        self.assertEqual(rec["message_id"], "<a@x>")
        self.assertEqual(rec["from"], {"name": "张三", "address": "zhang@x"})
        self.assertEqual(rec["to"], [{"name": "李四", "address": "li@x"}])
        self.assertEqual(rec["subject"], "测试主题")
        self.assertIn("正文内容", rec["body_text"])
        self.assertEqual(rec["date"], "2026-09-01T00:00:00+00:00")

    def test_references_extracted_and_dedup(self):
        rec = mailparser.parse_eml(
            make_eml(irt="<p@x>", refs=["<p@x>", "<g@x>", "<p@x>"]),
            "x.eml",
        )
        self.assertEqual(rec["in_reply_to"], "<p@x>")
        self.assertEqual(rec["references"], ["<p@x>", "<g@x>"])

    def test_missing_message_id(self):
        rec = mailparser.parse_eml(make_eml(msg_id=None), "x.eml")
        self.assertIsNone(rec["message_id"])

    def test_bad_date_recorded_not_fatal(self):
        raw = (
            b"Message-ID: <d@x>\r\nFrom: a@x\r\nTo: b@x\r\n"
            b"Subject: bad date\r\nDate: not-a-date-at-all\r\n\r\nbody"
        )
        rec = mailparser.parse_eml(raw, "x.eml")
        self.assertIsNone(rec["date"])
        self.assertTrue(any("Date" in i for i in rec["issues"]))

    def test_unknown_charset_falls_back_utf8(self):
        body = "真实的 UTF-8 正文".encode("utf-8")
        raw = (
            b"Message-ID: <bad@x>\r\nFrom: a@x\r\nTo: b@x\r\n"
            b"Subject: bad charset\r\n"
            b"Content-Type: text/plain; charset=x-no-such-charset\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            + base64.encodebytes(body)
        )
        rec = mailparser.parse_eml(raw, "x.eml")
        self.assertIn("真实的 UTF-8 正文", rec["body_text"])
        self.assertTrue(any("降级" in i for i in rec["issues"]))

    def test_undecodable_bytes_marked(self):
        # 声明为 us-ascii，但载荷不是 ASCII 且与任何候选都冲突时……
        # latin-1 永远能解码，所以这里验证“声明错误会被记录”
        body = b"\xff\xfe\xfd caff\xe9"
        raw = (
            b"Message-ID: <bad2@x>\r\nFrom: a@x\r\nTo: b@x\r\n"
            b"Subject: x\r\n"
            b"Content-Type: text/plain; charset=us-ascii\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            + base64.encodebytes(body)
        )
        rec = mailparser.parse_eml(raw, "x.eml")
        self.assertTrue(any("降级" in i for i in rec["issues"]))
        self.assertIn("caff", rec["body_text"])

    def test_attachment_metadata_only(self):
        msg = EmailMessage()
        msg["Message-ID"] = "<att@x>"
        msg["From"] = "a@x"
        msg["To"] = "b@x"
        msg["Subject"] = "带附件"
        msg["Date"] = format_datetime(datetime(2026, 9, 1, tzinfo=timezone.utc))
        msg.set_content("见附件")
        payload = b"%PDF-1.4 fake pdf bytes"
        msg.add_attachment(payload, maintype="application",
                           subtype="pdf", filename="合同.pdf")
        rec = mailparser.parse_eml(bytes(msg), "x.eml")
        self.assertEqual(len(rec["attachments"]), 1)
        att = rec["attachments"][0]
        self.assertEqual(att["filename"], "合同.pdf")
        self.assertEqual(att["content_type"], "application/pdf")
        self.assertEqual(att["size"], len(payload))
        self.assertEqual(att["sha256"], hashlib.sha256(payload).hexdigest())
        # 元数据里绝不包含二进制内容
        self.assertNotIn("data", att)
        self.assertNotIn("payload", att)

    def test_html_body_converted(self):
        msg = EmailMessage()
        msg["Message-ID"] = "<h@x>"
        msg["From"] = "a@x"
        msg["To"] = "b@x"
        msg["Subject"] = "html"
        msg["Date"] = format_datetime(datetime(2026, 9, 1, tzinfo=timezone.utc))
        msg.add_alternative(
            "<h2>标题</h2><p>正文<b>加粗</b></p><script>x=1</script>",
            subtype="html",
        )
        rec = mailparser.parse_eml(bytes(msg), "x.eml")
        self.assertTrue(rec["body_html_present"])
        self.assertIn("标题", rec["body_text"])
        self.assertIn("正文加粗", rec["body_text"])
        self.assertNotIn("x=1", rec["body_text"])

    def test_completely_broken_message_does_not_raise(self):
        rec = mailparser.parse_eml(b"\x00\x01\x02 not an email", "broken.eml")
        self.assertEqual(rec["source_file"], "broken.eml")
        self.assertTrue(rec["issues"])

    def test_raw_sha256_present(self):
        raw = make_eml()
        rec = mailparser.parse_eml(raw, "a.eml")
        self.assertEqual(rec["raw_sha256"], hashlib.sha256(raw).hexdigest())


if __name__ == "__main__":
    unittest.main()
