# itu-mail-mcp

A small MCP server that lets Claude (or any MCP client) read and organise my İTÜ mail.

I wrote it because I wanted to ask Claude things like "anything new from my instructors this week?" without copy-pasting from webmail. It talks plain IMAP to `imap.itu.edu.tr`, so it should work for any İTÜ account.

## What it can do

- `mail_status`: login check and INBOX counts
- `list_folders`, `list_messages`, `search_messages`: browse and search (Turkish text works)
- `get_message`: read a message as text, with its attachment list. Doesn't mark it as read.
- `download_attachment`: saves to `~/.itu_mail_state/attachments/<uid>/`
- `create_folder`, `move_message`, `rename_folder`, `delete_folder`, `flag_message`: organise mail
- `mark_as_read`, `save_draft`: only when `ITU_MAIL_ENABLE_WRITE=1` is set

## Security

- The host is hardcoded (`imap.itu.edu.tr:993`, TLS), so your password can't be sent anywhere else.
- The password lives in the macOS Keychain, not in a config file. `ITU_MAIL_PASSWORD` works as a fallback, but then it sits in plain text in your MCP config.
- There is no send tool. `save_draft` puts a draft in Drafts and you send it yourself from webmail.
- `delete_folder` only deletes empty folders. INBOX and system folders (Sent, Drafts, Trash, Junk) can't be renamed or deleted.
- Attachments are always saved under the state dir with `0600` permissions; file names from the mail can't escape that folder.
- The server tells the model to treat mail content as untrusted and not to follow instructions inside e-mails.

## Setup

macOS, Python 3.10+.

```bash
pip install --user -e .
security add-generic-password -s itu-mail-mcp -a <your-itu-username> -w
claude mcp add itu-mail -s user -e ITU_MAIL_USERNAME=<your-itu-username> -- itu-mail-mcp
```

The second command asks for your İTÜ password and stores it in the Keychain. If `itu-mail-mcp` isn't on your PATH, use the full path (e.g. `~/Library/Python/3.11/bin/itu-mail-mcp`).

## Extra: mail-sorter

[`examples/mail-sorter`](examples/mail-sorter) uses the same IMAP client to move mail into folders by simple rules, either once or continuously with IMAP IDLE.

## Tests

The tests use a fake IMAP connection, so they don't need an account or network.

```bash
PYTHONPATH=src python3 -m pytest -q tests
```

## License

MIT
