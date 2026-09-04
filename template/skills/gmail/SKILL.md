# Gmail Skill

Send, read, and modify Gmail for the operator's accounts. Tokens live in the macOS Keychain under
service `core-gmail` — never on disk.

## Accounts
Declare them in this Core's `.claude/identity.json` (`email.accounts`, `email.default_outbound`), e.g.
- `you@example.com` — the operator's own correspondence
- `assistant@example.com` — Core's own account, the DEFAULT outbound for anything Core composes

## Default outbound routing
- Default: Core's own account, body in Core's voice as the operator's assistant.
- The operator's own account ONLY when they explicitly say "send from my email".
- Voice stays Core's in either case; never ghostwrite as the operator.

## Setup (once per account)
```bash
python3 .claude/skills/gmail/setup.py you@example.com
python3 .claude/skills/gmail/setup.py assistant@example.com
```

## Usage
```python
from skills.gmail.gmail import send_email, list_messages, get_message, archive_message, mark_read
send_email("assistant@example.com", "someone@example.com", "Subject", "Body text")
messages = list_messages("you@example.com", query="from:someone@example.com", max_results=5)
```

## Privacy
- Scoped queries only — always pass a `query` filter, never bulk-read an inbox.
- Invoked only, scoped only, minimum necessary. Any automation using this skill needs a whitelist
  in `memory/automations/`.
- `get_message()` defaults to `format="metadata"` (headers only). `format="full"` is allowed only for
  senders on the trusted list in `memory/automations/gmail-<localpart>.md`; others raise `RuntimeError`.
