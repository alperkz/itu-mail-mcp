# mail-sorter

A small script on top of `itu_mail_mcp.imap_client` that I use to keep my İTÜ inbox clean. It moves mail into folders by simple rules (subject / sender name / sender address contains ...).

- `sort_mail.py`: sorts what's already in INBOX. Dry run by default, add `--apply` to actually move.
- `watch_mail.py`: stays connected with IMAP IDLE and sorts new mail as it arrives. It remembers the last UID it saw, so mail that came in while it was off gets sorted on the next start. `--once` runs a single pass and exits.

## Usage

Install the main package first (see the top-level README), then:

```bash
cd examples/mail-sorter
cp rules.example.json rules.json   # edit your rules
export ITU_MAIL_USERNAME=<your-itu-username>
python3 sort_mail.py               # see what would move
python3 sort_mail.py --apply
python3 watch_mail.py
```

Destination folders must already exist (create them with `create_folder` or in webmail). `rules.json` is git-ignored since it's personal.

To run the watcher in the background on macOS, fill in `itu-mail-sorter.plist.example`, copy it to `~/Library/LaunchAgents/` and `launchctl load` it.

## Resource use

I wanted to know whether leaving the watcher running drains the battery. Measured on my MacBook (Apple M4, ~60 Wh battery) in November 2026, after it had been running for 22 days under launchd:

| | Value | How |
|---|---|---|
| CPU time, 22 days total | 17.6 s (< 0.001% average) | `ps` |
| Memory | ~9–27 MB | `ps`, `top` |
| Network | ~10 KB/hour (12.7 KB in 78 min), no retransmits | `nettop` |
| CPU energy | ≤ ~90 J in 22 days (worst case: all of it at ~5 W) | estimate |
| Wi-Fi energy | ≤ ~25 J/day (a few packets every 5 min) | estimate |
| Battery share | ≤ ~0.3% of one full charge over 22 days | estimate |

The first three rows are measured. The energy rows are rough upper bounds: macOS doesn't report per-process Wi-Fi power, so the Wi-Fi figure assumes ~0.02–0.1 J per radio wake-up. While the Mac sleeps the watcher doesn't run at all; it reconnects on wake.

`IDLE_TIMEOUT` (300 s) sets how often it re-issues IDLE. Raising it means fewer wake-ups, but a dropped connection takes longer to notice.
