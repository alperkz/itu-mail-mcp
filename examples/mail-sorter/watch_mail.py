"""Watch INBOX with IMAP IDLE and move new mail by rules.json as it arrives.

On every start (and every IDLE cycle) it also sorts mail that arrived while it was not
running, using the last processed UID kept in ~/.itu_mail_state/sorter.json.
rules.json is re-read on each cycle, so rule edits apply without a restart.
--once processes pending mail and exits (for testing).
"""

from __future__ import annotations

import argparse
import imaplib
import json
import logging
import os
import select
import time
from collections import defaultdict
from pathlib import Path

from itu_mail_mcp.imap_client import MailClient, MailError

from sort_mail import FETCH_BATCH, MOVE_BATCH, classify, load_rules

FOLDER = "INBOX"
# Re-issue IDLE well under the 29-minute server limit; short enough that a dead
# connection (e.g. after the Mac wakes from sleep) is noticed within minutes.
IDLE_TIMEOUT = 300
MAX_BACKOFF = 300
STATE_PATH = Path(os.getenv("ITU_MAIL_STATE_DIR", "~/.itu_mail_state")).expanduser() / "sorter.json"

log = logging.getLogger("mail-sorter")


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {"uidvalidity": None, "last_uid": 0}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_PATH)


def process_new(watcher: MailClient, mover: MailClient, conn: imaplib.IMAP4_SSL, state: dict) -> None:
    last_uid = state["last_uid"]
    # "N:*" always returns the highest UID, even when it is below N.
    uids = [u for u in watcher._search_uids(conn, ["UID", f"{last_uid + 1}:*"], None) if int(u) > last_uid]
    if not uids:
        return
    rules = load_rules()
    groups: dict[str, list[str]] = defaultdict(list)
    for i in range(0, len(uids), FETCH_BATCH):
        for summary in watcher._fetch_summaries(conn, uids[i : i + FETCH_BATCH]):
            rule = classify(summary, rules)
            if rule:
                groups[rule["folder"]].append(summary["uid"])
    for folder, folder_uids in groups.items():
        for i in range(0, len(folder_uids), MOVE_BATCH):
            mover.move_messages(folder_uids[i : i + MOVE_BATCH], FOLDER, folder)
        log.info("%d yeni mail → %s", len(folder_uids), folder)
    kept = len(uids) - sum(len(v) for v in groups.values())
    if kept:
        log.info("%d yeni mail INBOX'ta kaldı", kept)
    state["last_uid"] = max(int(u) for u in uids)
    save_state(state)


def idle(conn: imaplib.IMAP4_SSL, timeout: float) -> bool:
    """Wait in IDLE until new mail (True) or timeout (False). Python 3.11 imaplib has no idle()."""
    tag = conn._new_tag()
    conn.send(tag + b" IDLE\r\n")
    line = conn.readline()
    if not line.startswith(b"+"):
        raise MailError(f"IDLE refused: {line!r}")
    new_mail = False
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        # A line already sitting in the read buffer is not seen by select(); the
        # worst case is that it is picked up at the next timeout.
        if not conn.sock.pending():
            ready, _, _ = select.select([conn.sock], [], [], remaining)
            if not ready:
                break
        line = conn.readline()
        if not line or line.startswith(b"* BYE"):
            raise ConnectionError("server closed the connection")
        if line.rstrip().endswith(b"EXISTS"):
            new_mail = True
            break
    conn.send(b"DONE\r\n")
    while True:
        line = conn.readline()
        if not line:
            raise ConnectionError("server closed the connection")
        if line.startswith(tag):
            if b" OK" not in line:
                raise MailError(f"IDLE ended badly: {line!r}")
            break
    conn.untagged_responses.clear()
    return new_mail


def run(once: bool) -> None:
    watcher, mover = MailClient(), MailClient()  # separate locks: moves open their own connection
    backoff = 10
    while True:
        try:
            with watcher.folder(FOLDER) as conn:
                state = load_state()
                uidvalidity = conn.response("UIDVALIDITY")[1][0].decode()
                if state["uidvalidity"] != uidvalidity:
                    state = {"uidvalidity": uidvalidity, "last_uid": 0}
                log.info("bağlandı; son işlenen UID %s", state["last_uid"])
                backoff = 10
                while True:
                    process_new(watcher, mover, conn, state)
                    if once:
                        return
                    idle(conn, IDLE_TIMEOUT)
        except (OSError, imaplib.IMAP4.error, MailError, ConnectionError) as exc:
            if once:
                raise
            log.warning("bağlantı hatası: %s; %d sn sonra yeniden denenecek", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="process pending mail and exit")
    args = parser.parse_args()
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    run(args.once)


if __name__ == "__main__":
    main()
