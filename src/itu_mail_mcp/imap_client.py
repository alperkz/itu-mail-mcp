"""Thin IMAP client for İTÜ mail (Dovecot at imap.itu.edu.tr)."""

from __future__ import annotations

import base64
import email
import email.utils
import imaplib
import os
import re
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import date
from email import policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterator

from bs4 import BeautifulSoup

from .credentials import Credentials, load_credentials

# Host is fixed on purpose: credentials are only ever sent to İTÜ's IMAP server.
IMAP_HOST = "imap.itu.edu.tr"
IMAP_PORT = 993
DEFAULT_TIMEOUT = 30
HEADER_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID"
# Folders the mail clients rely on; they are never renamed or deleted.
SPECIAL_USE_FLAGS = {"\\Sent", "\\Drafts", "\\Trash", "\\Junk", "\\Archive", "\\All"}


class MailError(RuntimeError):
    pass


# --------------------------------------------------------------- modified UTF-7


def decode_mutf7(name: str) -> str:
    """Decode an IMAP folder name (RFC 3501 modified UTF-7), e.g. 'Gelen &AT8-...'."""

    def _decode(match: re.Match[str]) -> str:
        chunk = match.group(1)
        if not chunk:
            return "&"
        chunk = chunk.replace(",", "/")
        chunk += "=" * (-len(chunk) % 4)
        return base64.b64decode(chunk).decode("utf-16-be")

    return re.sub(r"&([^-]*)-", _decode, name)


def encode_mutf7(name: str) -> str:
    out: list[str] = []
    buffer: list[str] = []

    def _flush() -> None:
        if buffer:
            raw = "".join(buffer).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode().rstrip("=").replace("/", ",") + "-")
            buffer.clear()

    for char in name:
        if 0x20 <= ord(char) <= 0x7E:
            _flush()
            out.append("&-" if char == "&" else char)
        else:
            buffer.append(char)
    _flush()
    return "".join(out)


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise MailError(f"Dates must be YYYY-MM-DD, got {value!r}.") from exc
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"{parsed.day:02d}-{months[parsed.month - 1]}-{parsed.year}"


# ------------------------------------------------------------------ parsing


_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.*)$')


def _parse_list_line(item: Any) -> dict[str, Any] | None:
    if isinstance(item, tuple):  # name sent as a literal
        meta, name_bytes = item
        match = _LIST_RE.match(meta)
        raw_name = name_bytes.decode()
    else:
        if item is None:
            return None
        match = _LIST_RE.match(item)
        raw_name = match.group("name").decode() if match else ""
    if not match:
        return None
    if raw_name.startswith('"') and raw_name.endswith('"'):
        raw_name = raw_name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    flags = match.group("flags").decode().split()
    delim = match.group("delim").decode()
    return {
        "name": decode_mutf7(raw_name),
        "flags": flags,
        "delimiter": None if delim == "NIL" else delim[1:-1],
    }


def _group_fetch(data: list[Any]) -> list[tuple[bytes, bytes]]:
    """Turn imaplib FETCH output into (metadata, literal) pairs.

    Metadata after the literal (e.g. a trailing ' FLAGS (...))') is appended to the
    pair's metadata so UID/FLAGS are found wherever the server put them.
    """
    records: list[list[bytes]] = []
    for item in data:
        if isinstance(item, tuple):
            records.append([item[0], item[1]])
        elif isinstance(item, bytes) and records:
            records[-1][0] += b" " + item
    return [(meta, literal) for meta, literal in records]


def _meta_value(meta: bytes, key: bytes) -> str | None:
    match = re.search(key + rb" (\d+)", meta)
    return match.group(1).decode() if match else None


def _meta_flags(meta: bytes) -> list[str]:
    match = re.search(rb"FLAGS \(([^)]*)\)", meta)
    return match.group(1).decode().split() if match else []


def _header(msg: EmailMessage, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:  # malformed header
        value = msg.get_all(name, failobj=[""])[0] if name in msg else ""
    # Folded headers (e.g. "Message-ID:\r\n <...>") keep the fold's leading space.
    return str(value).strip() if value is not None else ""


def _iso_date(raw: str) -> str | None:
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw).isoformat()
    except (TypeError, ValueError):
        return raw


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    text = soup.get_text("\n")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _summary(uid: str, flags: list[str], size: str | None, header_bytes: bytes) -> dict[str, Any]:
    msg = email.message_from_bytes(header_bytes, policy=policy.default)
    return {
        "uid": uid,
        "date": _iso_date(_header(msg, "Date")),
        "from": _header(msg, "From"),
        "to": _header(msg, "To"),
        "cc": _header(msg, "Cc") or None,
        "subject": _header(msg, "Subject"),
        "unread": "\\Seen" not in flags,
        "flagged": "\\Flagged" in flags,
        "size": int(size) if size else None,
    }


def _safe_filename(name: str | None, fallback: str) -> str:
    cleaned = Path(name or "").name.strip().replace("\x00", "")
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", cleaned).lstrip(".")
    return cleaned[:150] or fallback


# ------------------------------------------------------------------- client


class MailClient:
    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = Path(
            state_dir or os.getenv("ITU_MAIL_STATE_DIR", "~/.itu_mail_state")
        ).expanduser()
        self._lock = threading.Lock()

    @contextmanager
    def connect(self, credentials: Credentials | None = None) -> Iterator[imaplib.IMAP4_SSL]:
        credentials = credentials or load_credentials()
        with self._lock:
            try:
                conn = imaplib.IMAP4_SSL(
                    IMAP_HOST,
                    IMAP_PORT,
                    ssl_context=ssl.create_default_context(),
                    timeout=DEFAULT_TIMEOUT,
                )
            except OSError as exc:
                raise MailError(f"Cannot reach {IMAP_HOST}: {exc}") from exc
            try:
                try:
                    conn.login(credentials.username, credentials.password)
                except imaplib.IMAP4.error as exc:
                    raise MailError(f"İTÜ mail login failed: {exc}") from exc
                yield conn
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

    @contextmanager
    def folder(self, name: str, readonly: bool = True) -> Iterator[imaplib.IMAP4_SSL]:
        with self.connect() as conn:
            status, data = conn.select(_quote(encode_mutf7(name)), readonly=readonly)
            if status != "OK":
                raise MailError(f"Cannot open folder {name!r}: {data}")
            yield conn

    # -------------------------------------------------------------- read

    def status(self) -> dict[str, Any]:
        credentials = load_credentials()
        with self.connect(credentials) as conn:
            _, data = conn.status("INBOX", "(MESSAGES UNSEEN)")
        counts = dict(re.findall(rb"(MESSAGES|UNSEEN) (\d+)", data[0]))
        return {
            "authenticated": True,
            "host": IMAP_HOST,
            "username": credentials.username,
            "password_source": credentials.source,
            "inbox_messages": int(counts.get(b"MESSAGES", 0)),
            "inbox_unread": int(counts.get(b"UNSEEN", 0)),
        }

    def list_folders(self, include_counts: bool = False) -> list[dict[str, Any]]:
        with self.connect() as conn:
            _, data = conn.list()
            folders = [f for f in (_parse_list_line(item) for item in data) if f]
            for folder in folders:
                folder.pop("delimiter", None)
            if include_counts:
                for folder in folders:
                    if "\\Noselect" in folder["flags"]:
                        continue
                    status, info = conn.status(
                        _quote(encode_mutf7(folder["name"])), "(MESSAGES UNSEEN)"
                    )
                    if status == "OK":
                        counts = dict(re.findall(rb"(MESSAGES|UNSEEN) (\d+)", info[0]))
                        folder["messages"] = int(counts.get(b"MESSAGES", 0))
                        folder["unread"] = int(counts.get(b"UNSEEN", 0))
        return folders

    def _fetch_summaries(self, conn: imaplib.IMAP4_SSL, uids: list[str]) -> list[dict[str, Any]]:
        if not uids:
            return []
        _, data = conn.uid(
            "FETCH",
            ",".join(uids),
            f"(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])",
        )
        by_uid = {}
        for meta, literal in _group_fetch(data):
            uid = _meta_value(meta, b"UID")
            if uid:
                by_uid[uid] = _summary(uid, _meta_flags(meta), _meta_value(meta, b"RFC822.SIZE"), literal)
        return [by_uid[uid] for uid in uids if uid in by_uid]

    def _search_uids(self, conn: imaplib.IMAP4_SSL, criteria: list[str], literal: str | None) -> list[str]:
        args = ["CHARSET", "UTF-8", *criteria] if literal is not None else criteria
        if literal is not None:
            conn.literal = literal.encode("utf-8")
        status, data = conn.uid("SEARCH", *args)
        if status != "OK":
            raise MailError(f"Search failed: {data}")
        return data[0].decode().split() if data and data[0] else []

    def list_messages(
        self, folder: str = "INBOX", limit: int = 20, offset: int = 0, unread_only: bool = False
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        with self.folder(folder) as conn:
            uids = self._search_uids(conn, ["UNSEEN" if unread_only else "ALL"], None)
            uids.sort(key=int, reverse=True)
            page = uids[offset : offset + limit]
            messages = self._fetch_summaries(conn, page)
        return {"folder": folder, "total": len(uids), "offset": offset, "messages": messages}

    def search(
        self,
        folder: str = "INBOX",
        text: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        subject: str | None = None,
        since: str | None = None,
        before: str | None = None,
        unread_only: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        criteria: list[str] = []
        if unread_only:
            criteria.append("UNSEEN")
        if since:
            criteria += ["SINCE", _imap_date(since)]
        if before:
            criteria += ["BEFORE", _imap_date(before)]

        # imaplib sends at most one literal, and only at the end of the command,
        # so the single non-ASCII value (common with Turkish text) goes last.
        literal: str | None = None
        text_fields = [("TEXT", text), ("FROM", from_), ("TO", to), ("SUBJECT", subject)]
        for key, value in text_fields:
            if not value:
                continue
            if value.isascii():
                criteria += [key, _quote(value)]
            elif literal is None:
                literal_key, literal = key, value
            else:
                raise MailError(
                    "Only one search field may contain non-ASCII (e.g. Turkish) characters; "
                    "narrow the others to ASCII or search in two steps."
                )
        if literal is not None:
            criteria.append(literal_key)
        elif any(value for _, value in text_fields):
            criteria = ["CHARSET", "UTF-8", *criteria]
        if not criteria:
            criteria = ["ALL"]

        with self.folder(folder) as conn:
            uids = self._search_uids(conn, criteria, literal)
            uids.sort(key=int, reverse=True)
            messages = self._fetch_summaries(conn, uids[:limit])
        return {"folder": folder, "total_matches": len(uids), "messages": messages}

    def _fetch_message(self, conn: imaplib.IMAP4_SSL, uid: str) -> tuple[EmailMessage, list[str]]:
        if not uid.isdigit():
            raise MailError("uid must be numeric.")
        _, data = conn.uid("FETCH", uid, "(UID FLAGS BODY.PEEK[])")
        records = _group_fetch(data)
        if not records:
            raise MailError(f"No message with UID {uid} in this folder.")
        meta, raw = records[0]
        return email.message_from_bytes(raw, policy=policy.default), _meta_flags(meta)

    def get_message(self, uid: str, folder: str = "INBOX", max_chars: int = 20000) -> dict[str, Any]:
        with self.folder(folder) as conn:
            msg, flags = self._fetch_message(conn, uid)

        body_part = msg.get_body(preferencelist=("plain", "html"))
        body, body_format = "", None
        if body_part is not None:
            try:
                content = body_part.get_content()
            except (LookupError, UnicodeDecodeError):
                content = body_part.get_payload(decode=True).decode("utf-8", "replace")
            if body_part.get_content_subtype() == "html":
                body, body_format = _html_to_text(content), "html→text"
            else:
                body, body_format = content.strip(), "plain"
        body = body.replace("\r\n", "\n").replace("\r", "\n")
        truncated = len(body) > max_chars

        return {
            "uid": uid,
            "folder": folder,
            "date": _iso_date(_header(msg, "Date")),
            "from": _header(msg, "From"),
            "to": _header(msg, "To"),
            "cc": _header(msg, "Cc") or None,
            "reply_to": _header(msg, "Reply-To") or None,
            "subject": _header(msg, "Subject"),
            "message_id": _header(msg, "Message-ID") or None,
            "unread": "\\Seen" not in flags,
            "body_format": body_format,
            "body": body[:max_chars],
            "truncated": truncated,
            "attachments": [
                {
                    "index": index,
                    "filename": part.get_filename(),
                    "content_type": part.get_content_type(),
                    "size": len(part.get_payload(decode=True) or b""),
                }
                for index, part in enumerate(msg.iter_attachments())
            ],
        }

    def download_attachment(self, uid: str, index: int, folder: str = "INBOX") -> dict[str, Any]:
        with self.folder(folder) as conn:
            msg, _ = self._fetch_message(conn, uid)
        attachments = list(msg.iter_attachments())
        if not 0 <= index < len(attachments):
            raise MailError(f"Message {uid} has {len(attachments)} attachment(s); index {index} is invalid.")
        part = attachments[index]
        payload = part.get_payload(decode=True) or b""

        # Always under the state dir: a mail cannot steer a write elsewhere on disk.
        target_dir = self.state_dir / "attachments" / uid
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        filename = _safe_filename(part.get_filename(), f"attachment-{index}")
        target = target_dir / filename
        stem, suffix, n = target.stem, target.suffix, 1
        while target.exists():
            target = target_dir / f"{stem} ({n}){suffix}"
            n += 1
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        return {"path": str(target), "content_type": part.get_content_type(), "size": len(payload)}

    # ------------------------------------------------------------- write

    def _folder_index(self, conn: imaplib.IMAP4_SSL) -> dict[str, dict[str, Any]]:
        _, data = conn.list()
        return {f["name"]: f for f in (_parse_list_line(item) for item in data) if f}

    def create_folder(self, name: str, parent: str | None = None) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise MailError("Folder name must not be empty.")
        with self.connect() as conn:
            folders = self._folder_index(conn)
            delimiter = next((f["delimiter"] for f in folders.values() if f["delimiter"]), "/")
            if parent:
                if parent not in folders:
                    raise MailError(f"Parent folder {parent!r} does not exist.")
                if delimiter in name:
                    raise MailError(f"With parent set, name must not contain {delimiter!r}.")
                full_name = f"{parent}{delimiter}{name}"
            else:
                full_name = name
            if full_name in folders:
                return {"folder": full_name, "created": False, "note": "Folder already exists."}
            mailbox = _quote(encode_mutf7(full_name))
            status, data = conn.create(mailbox)
            if status != "OK":
                raise MailError(f"Could not create folder {full_name!r}: {data}")
            conn.subscribe(mailbox)
        return {"folder": full_name, "created": True, "delimiter": delimiter}

    def _check_changeable(self, name: str, folders: dict[str, dict[str, Any]]) -> dict[str, Any]:
        folder = folders.get(name)
        if folder is None:
            raise MailError(f"Folder {name!r} does not exist.")
        special = [flag for flag in folder["flags"] if flag in SPECIAL_USE_FLAGS]
        if name.upper() == "INBOX" or special:
            raise MailError(f"{name!r} is a system folder ({', '.join(special) or 'INBOX'}); leave it as is.")
        return folder

    def delete_folder(self, name: str) -> dict[str, Any]:
        with self.connect() as conn:
            folders = self._folder_index(conn)
            folder = self._check_changeable(name, folders)
            delimiter = folder["delimiter"] or "."
            children = [f for f in folders if f.startswith(name + delimiter)]
            if children:
                raise MailError(f"{name!r} has subfolders ({', '.join(children)}); delete or move them first.")
            mailbox = _quote(encode_mutf7(name))
            status, data = conn.status(mailbox, "(MESSAGES)")
            if status != "OK":
                raise MailError(f"Cannot read folder {name!r}: {data}")
            count = int(dict(re.findall(rb"(MESSAGES) (\d+)", data[0])).get(b"MESSAGES", 0))
            if count:
                raise MailError(f"{name!r} still holds {count} message(s); only empty folders can be deleted.")
            conn.unsubscribe(mailbox)
            status, data = conn.delete(mailbox)
            if status != "OK":
                raise MailError(f"Could not delete folder {name!r}: {data}")
        return {"deleted": name}

    def rename_folder(self, name: str, new_name: str) -> dict[str, Any]:
        new_name = new_name.strip()
        if not new_name:
            raise MailError("New folder name must not be empty.")
        with self.connect() as conn:
            folders = self._folder_index(conn)
            self._check_changeable(name, folders)
            if new_name in folders:
                raise MailError(f"A folder named {new_name!r} already exists.")
            old_box, new_box = _quote(encode_mutf7(name)), _quote(encode_mutf7(new_name))
            status, data = conn.rename(old_box, new_box)
            if status != "OK":
                raise MailError(f"Could not rename {name!r} to {new_name!r}: {data}")
            conn.unsubscribe(old_box)
            conn.subscribe(new_box)
        return {"renamed": name, "to": new_name}

    def move_messages(self, uids: list[str], source: str, destination: str) -> dict[str, Any]:
        if not uids or not all(uid.isdigit() for uid in uids):
            raise MailError("uids must be a non-empty list of numeric UIDs.")
        if source == destination:
            raise MailError("Source and destination folders are the same.")
        with self.connect() as conn:
            if destination not in self._folder_index(conn):
                raise MailError(f"Destination folder {destination!r} does not exist; create it first.")
            status, data = conn.select(_quote(encode_mutf7(source)), readonly=False)
            if status != "OK":
                raise MailError(f"Cannot open folder {source!r}: {data}")
            present = set(self._search_uids(conn, ["UID", ",".join(uids)], None))
            found = [uid for uid in uids if uid in present]
            missing = [uid for uid in uids if uid not in present]
            # imaplib keeps the pre-login list; MOVE/UIDPLUS are only advertised after login.
            _, caps = conn.capability()
            capabilities = set(caps[-1].decode().upper().split()) if caps and caps[-1] else set()
            if found:
                target = _quote(encode_mutf7(destination))
                uid_set = ",".join(found)
                if "MOVE" in capabilities:
                    status, data = conn.uid("MOVE", uid_set, target)
                    if status != "OK":
                        raise MailError(f"Move failed: {data}")
                elif "UIDPLUS" in capabilities:
                    # UID EXPUNGE removes only these messages, never other \Deleted mail.
                    status, data = conn.uid("COPY", uid_set, target)
                    if status != "OK":
                        raise MailError(f"Copy failed: {data}")
                    status, data = conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
                    if status == "OK":
                        status, data = conn.uid("EXPUNGE", uid_set)
                    if status != "OK":
                        raise MailError(
                            f"Copied to {destination!r} but could not remove from {source!r}: {data}"
                        )
                else:
                    raise MailError("Server supports neither MOVE nor UIDPLUS; cannot move safely.")
        return {"from": source, "to": destination, "moved": found, "not_found": missing}

    def set_read(self, uids: list[str], read: bool = True, folder: str = "INBOX") -> dict[str, Any]:
        if not uids or not all(uid.isdigit() for uid in uids):
            raise MailError("uids must be a non-empty list of numeric UIDs.")
        with self.folder(folder, readonly=False) as conn:
            status, data = conn.uid("STORE", ",".join(uids), "+FLAGS" if read else "-FLAGS", "(\\Seen)")
        if status != "OK":
            raise MailError(f"Could not update flags: {data}")
        return {"folder": folder, "uids": uids, "read": read}

    def set_flagged(self, uids: list[str], flagged: bool = True, folder: str = "INBOX") -> dict[str, Any]:
        if not uids or not all(uid.isdigit() for uid in uids):
            raise MailError("uids must be a non-empty list of numeric UIDs.")
        with self.folder(folder, readonly=False) as conn:
            status, data = conn.uid("STORE", ",".join(uids), "+FLAGS" if flagged else "-FLAGS", "(\\Flagged)")
        if status != "OK":
            raise MailError(f"Could not update flags: {data}")
        return {"folder": folder, "uids": uids, "flagged": flagged}

    def save_draft(self, to: str, subject: str, body: str, cc: str | None = None) -> dict[str, Any]:
        credentials = load_credentials()
        sender = credentials.username if "@" in credentials.username else f"{credentials.username}@itu.edu.tr"
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain="itu.edu.tr")
        msg.set_content(body)

        drafts = next(
            (f["name"] for f in self.list_folders() if "\\Drafts" in f["flags"]),
            "Drafts",
        )
        with self.connect() as conn:
            status, data = conn.append(
                _quote(encode_mutf7(drafts)),
                "(\\Draft \\Seen)",
                imaplib.Time2Internaldate(time.time()),
                msg.as_bytes(),
            )
        if status != "OK":
            raise MailError(f"Could not save draft: {data}")
        return {"saved_to": drafts, "to": to, "subject": subject, "note": "Not sent. Review and send it from webmail."}
