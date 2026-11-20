"""Offline tests: a fake IMAP connection stands in for imap.itu.edu.tr."""

from contextlib import contextmanager
from email import policy
from email.message import EmailMessage

import pytest

from itu_mail_mcp import imap_client
from itu_mail_mcp.imap_client import MailClient, MailError, decode_mutf7, encode_mutf7


def _raw_message() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Öğrenci İşleri <registrar@example.edu>"
    msg["To"] = "student@example.edu"
    msg["Subject"] = "Ders kaydı hakkında"
    msg["Date"] = "Wed, 29 Oct 2026 10:00:00 +0300"
    msg.set_content("Düz metin\ngövde")
    msg.add_alternative("<html><body><p>HTML <b>gövde</b></p><script>x()</script></body></html>", subtype="html")
    msg.add_attachment(b"%PDF-1.4 test", maintype="application", subtype="pdf", filename="../../evil/program.pdf")
    # Real servers send CRLF and fold long headers onto a continuation line.
    return b"Message-ID:\r\n <abc@example.edu>\r\n" + msg.as_bytes(policy=policy.SMTP)


class FakeConn:
    def __init__(self) -> None:
        self.literal = None
        self.calls = []
        self.raw = _raw_message()
        self.header = b"From: =?utf-8?q?=C3=96=C4=9Frenci?= <registrar@example.edu>\r\nSubject: Merhaba\r\nDate: Wed, 29 Oct 2026 10:00:00 +0300\r\n\r\n"

    def uid(self, command, *args):
        self.calls.append((command, args, self.literal))
        self.literal = None
        if command == "SEARCH":
            return "OK", [b"3 12 7"]
        if command == "FETCH" and "BODY.PEEK[]" in args[1]:
            return "OK", [(b"1 (UID 12 FLAGS (\\Seen) BODY[] {%d}" % len(self.raw), self.raw), b")"]
        if command == "FETCH":
            return "OK", [
                (b"1 (UID 12 RFC822.SIZE 900 BODY[HEADER.FIELDS (FROM)] {10}", self.header),
                b" FLAGS (\\Flagged))",
                (b"2 (UID 7 FLAGS () RFC822.SIZE 100 BODY[HEADER.FIELDS (FROM)] {10}", self.header),
                b")",
            ]
        return "OK", [None]

    def select(self, mailbox, readonly=True):
        self.calls.append(("SELECT", (mailbox, readonly), None))
        return "OK", [b"3"]

    def list(self):
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Drafts) "/" Drafts',
            (b'(\\HasNoChildren) "/" {14}', b"&AMc-al&ATE-_ma"),
        ]


@pytest.fixture
def client(tmp_path, monkeypatch):
    conn = FakeConn()
    c = MailClient(state_dir=tmp_path)

    @contextmanager
    def fake_folder(name, readonly=True):
        yield conn

    monkeypatch.setattr(c, "folder", fake_folder)
    monkeypatch.setattr(c, "connect", lambda: fake_folder("INBOX"))
    c.conn = conn
    return c


def test_mutf7_roundtrip():
    for name in ["INBOX", "Gönderilmiş Öğeler", "A&B", "Çalışma/İş"]:
        assert decode_mutf7(encode_mutf7(name)) == name
    assert encode_mutf7("A&B") == "A&-B"


def test_list_folders(client):
    names = [f["name"] for f in client.list_folders()]
    assert names == ["INBOX", "Drafts", "Çalı_ma"]


def test_list_messages_newest_first_and_trailing_flags(client):
    result = client.list_messages(limit=2)
    assert result["total"] == 3
    assert [m["uid"] for m in result["messages"]] == ["12", "7"]
    first = result["messages"][0]
    assert first["flagged"] and first["unread"]
    assert first["from"].startswith("Öğrenci")


def test_search_turkish_goes_as_literal(client):
    client.search(subject="ödev", from_="hoca", since="2026-10-01")
    command, args, literal = client.conn.calls[0]
    assert args[:2] == ("CHARSET", "UTF-8")
    assert args[-1] == "SUBJECT" and literal == "ödev".encode()
    assert "01-Oct-2026" in args


def test_search_rejects_two_turkish_fields(client):
    with pytest.raises(MailError):
        client.search(subject="ödev", text="sınav")


def test_get_message_prefers_plain_and_lists_attachments(client):
    msg = client.get_message("12")
    assert msg["body"] == "Düz metin\ngövde" and msg["body_format"] == "plain"
    assert msg["message_id"] == "<abc@example.edu>"
    assert msg["unread"] is False
    assert msg["attachments"][0]["content_type"] == "application/pdf"


def test_download_attachment_stays_in_state_dir(client, tmp_path):
    first = client.download_attachment("12", 0)
    second = client.download_attachment("12", 0)
    assert first["path"].startswith(str(tmp_path / "attachments" / "12"))
    assert first["path"].endswith("program.pdf") and second["path"].endswith("program (1).pdf")
    import os, stat
    assert stat.S_IMODE(os.stat(first["path"]).st_mode) == 0o600


def test_html_to_text():
    assert imap_client._html_to_text("<p>a</p><script>bad()</script><p>b</p>") == "a\nb"
