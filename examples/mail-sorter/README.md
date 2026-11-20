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
