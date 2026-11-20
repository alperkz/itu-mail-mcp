"""Apply rules.json to a folder (INBOX by default).

Dry run by default: prints how many mails each rule would move. Pass --apply to move.
Uses the itu-mail-mcp IMAP client, so credentials come from the same Keychain entry
(ITU_MAIL_USERNAME must be set).
"""

from __future__ import annotations

import argparse
import email.utils
import json
from collections import defaultdict
from pathlib import Path

from itu_mail_mcp.imap_client import MailClient

RULES_PATH = Path(__file__).with_name("rules.json")
FETCH_BATCH = 300
MOVE_BATCH = 200


def load_rules(path: Path = RULES_PATH) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["rules"]


def match(rule: dict, summary: dict) -> bool:
    name, addr = email.utils.parseaddr(summary.get("from") or "")
    checks = {
        "subject_contains": summary.get("subject") or "",
        "from_name_contains": name,
        "from_addr_contains": addr,
    }
    conditions = [(key, rule[key]) for key in checks if key in rule]
    return bool(conditions) and all(
        value.casefold() in checks[key].casefold() for key, value in conditions
    )


def classify(summary: dict, rules: list[dict]) -> dict | None:
    return next((rule for rule in rules if match(rule, summary)), None)


def fetch_all(client: MailClient, folder: str) -> list[dict]:
    with client.folder(folder) as conn:
        uids = client._search_uids(conn, ["ALL"], None)
        summaries: list[dict] = []
        for i in range(0, len(uids), FETCH_BATCH):
            summaries += client._fetch_summaries(conn, uids[i : i + FETCH_BATCH])
    return summaries


def plan(summaries: list[dict], rules: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for summary in summaries:
        rule = classify(summary, rules)
        if rule:
            groups[rule["folder"]].append(summary)
    return groups


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", default="INBOX")
    parser.add_argument("--apply", action="store_true", help="actually move the mail")
    parser.add_argument("--samples", type=int, default=3, help="example subjects per folder")
    args = parser.parse_args()

    client = MailClient()
    rules = load_rules()
    summaries = fetch_all(client, args.folder)
    # Mail already in its rule's folder stays put.
    groups = {f: items for f, items in plan(summaries, rules).items() if f != args.folder}

    matched = sum(len(items) for items in groups.values())
    print(f"{args.folder}: {len(summaries)} mail, {matched} kurala uyuyor, {len(summaries) - matched} yerinde kalır")
    for folder, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(items):5d} → {folder}")
        for item in items[: args.samples]:
            print(f"          · {item['from'][:40]} | {(item['subject'] or '')[:70]}")

    if not args.apply:
        print("Deneme çalışması; hiçbir mail taşınmadı. Taşımak için --apply ekleyin.")
        return

    for folder, items in groups.items():
        uids = [item["uid"] for item in items]
        moved = missing = 0
        for i in range(0, len(uids), MOVE_BATCH):
            result = client.move_messages(uids[i : i + MOVE_BATCH], args.folder, folder)
            moved += len(result["moved"])
            missing += len(result["not_found"])
        print(f"  taşındı: {moved:5d} → {folder}" + (f" ({missing} bulunamadı)" if missing else ""))


if __name__ == "__main__":
    main()
