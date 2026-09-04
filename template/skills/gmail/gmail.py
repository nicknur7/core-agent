#!/usr/bin/env python3
"""
Gmail skill — send, read, and modify emails for Core.
Loads tokens from macOS Keychain; never writes credentials to disk.
"""

import json
import base64
import email.utils
import mimetypes
import os
import keyring
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

KEYRING_SERVICE = "core-gmail"
# File fallback for sandboxed Core sessions, which cannot read the macOS Keychain
# (same wall the git credential.store fix addressed). Keychain stays primary; this
# is only consulted when keyring can't return the token. chmod 600.
TOKEN_FALLBACK_DIR = os.environ.get("CORE_GMAIL_TOKEN_DIR") or os.path.join(
    os.path.expanduser("~"), ".config", "core-gmail")
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
AUTOMATIONS_DIR = os.path.join(os.environ.get("CORE_INSTANCE") or _REPO_ROOT, "memory/automations")

# Untrusted Reader infrastructure was archived 2026-05-13 (see
# tasks/decision-untrusted-reader-2026-05-13.md). For non-trusted senders we now
# refuse to return a full body rather than silently piping raw HTML into the
# model context. Caller can fall back to format="metadata" or expand the
# Trusted senders list in memory/automations/gmail-<localpart>.md (one sender
# at a time, with the operator's approval).


def _load_trusted_senders(email: str) -> set:
    localpart = email.split("@")[0]
    path = os.path.join(AUTOMATIONS_DIR, f"gmail-{localpart}.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return set()

    in_section = False
    trusted = set()
    for line in lines:
        stripped = line.rstrip("\n")
        if stripped.strip() == "## Trusted senders (gates raw-body reads)":
            in_section = True
            continue
        if in_section:
            if stripped.startswith("## "):
                break
            if stripped.startswith("- "):
                entry = stripped[2:]
                entry = entry.split("#")[0].strip().lower()
                if entry:
                    trusted.add(entry)
    return trusted


def _extract_body_text(payload: dict) -> str:
    plain_parts = []
    html_parts = []

    def walk(part):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if mime == "text/plain" and data:
            plain_parts.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace"))
        elif mime == "text/html" and data:
            html_parts.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace"))
        for subpart in part.get("parts", []):
            walk(subpart)

    walk(payload)
    if plain_parts:
        return "\n".join(plain_parts)
    return "\n".join(html_parts)


def _extract_sender(headers: list) -> str:
    for h in headers:
        if h.get("name", "").lower() == "from":
            _, addr = email.utils.parseaddr(h.get("value", ""))
            return addr.lower()
    return ""


def _token_file(email: str) -> str:
    return os.path.join(TOKEN_FALLBACK_DIR, f"{email}.json")


def _read_token(email: str):
    """Keychain first (interactive/Terminal); fall back to a session-readable file.
    A Claude Code session can't read the Keychain, so without the file it can't
    authenticate. Returns (token_json, source)."""
    try:
        tok = keyring.get_password(KEYRING_SERVICE, email)
        if tok:
            return tok, "keyring"
    except Exception:
        pass
    fpath = _token_file(email)
    if os.path.exists(fpath):
        with open(fpath, "r", encoding="utf-8") as f:
            return f.read(), "file"
    return None, None


def _persist_token(email: str, data: dict, source: str):
    """Persist a refreshed token. Best-effort to Keychain; to the file when the file
    path is the one in use, so a session's refresh survives."""
    try:
        keyring.set_password(KEYRING_SERVICE, email, json.dumps(data))
    except Exception:
        pass
    fpath = _token_file(email)
    if source == "file" or os.path.exists(fpath):
        try:
            os.makedirs(TOKEN_FALLBACK_DIR, exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.chmod(fpath, 0o600)
        except Exception:
            pass


def _get_service(email: str):
    token_json, source = _read_token(email)
    if not token_json:
        raise RuntimeError(
            f"No token found for {email}. Run setup.py first, or (for a sandboxed "
            f"session that can't read the Keychain) place creds at {_token_file(email)}."
        )

    data = json.loads(token_json)
    creds = Credentials(
        token=data["token"],
        refresh_token=data["refresh_token"],
        token_uri=data["token_uri"],
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=data["scopes"],
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        data["token"] = creds.token
        _persist_token(email, data, source)

    return build("gmail", "v1", credentials=creds)


def send_email(from_email: str, to: str, subject: str, body: str, html: bool = False, attachments: list = None,
               thread_id: str = None, in_reply_to: str = None):
    """thread_id + in_reply_to (2026-06-09): pass the original message's threadId and
    its Message-ID header to make a reply thread properly. Without them Gmail only
    groups by subject (one reply went out subject-threaded only).
    Typical use: orig = get_message(acct, mid); thread_id = orig["threadId"];
    in_reply_to = header "Message-ID" from orig payload headers."""
    service = _get_service(from_email)

    if attachments:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, "html" if html else "plain"))
        for path in attachments:
            ctype, encoding = mimetypes.guess_type(path)
            if ctype is None or encoding is not None:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            with open(path, "rb") as f:
                part = MIMEBase(maintype, subtype)
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=os.path.basename(path))
            msg.attach(part)
    elif html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "html"))
    else:
        msg = MIMEText(body)

    msg["to"] = to
    msg["from"] = from_email
    msg["subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    result = service.users().messages().send(userId="me", body=payload).execute()
    return result


def list_messages(email: str, query: str = "", max_results: int = 10):
    if not query or not query.strip():
        raise ValueError(
            "list_messages requires a non-empty query. "
            "Pass an explicit filter (e.g. query='from:someone@example.com') "
            "to prevent bulk inbox dumps."
        )
    service = _get_service(email)
    result = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    return result.get("messages", [])


def get_message(email: str, message_id: str, format: str = "metadata"):
    service = _get_service(email)
    response = service.users().messages().get(
        userId="me", id=message_id, format=format
    ).execute()

    if format != "full":
        return response

    headers = response.get("payload", {}).get("headers", [])
    sender = _extract_sender(headers)
    trusted = _load_trusted_senders(email)

    if sender in trusted:
        response["_core_sanitized"] = False
        return response

    raise RuntimeError(
        f"Refusing format='full' for non-trusted sender {sender!r}. "
        "Untrusted Reader was archived 2026-05-13 — no sanitization path is "
        "available for raw bodies. Either re-fetch with format='metadata', or "
        "ask the operator to add the sender to "
        f"memory/automations/gmail-{email.split('@')[0]}.md Trusted senders."
    )


def modify_labels(email: str, message_id: str, add_labels: list = None, remove_labels: list = None):
    service = _get_service(email)
    body = {
        "addLabelIds": add_labels or [],
        "removeLabelIds": remove_labels or [],
    }
    return service.users().messages().modify(
        userId="me", id=message_id, body=body
    ).execute()


def archive_message(email: str, message_id: str):
    return modify_labels(email, message_id, remove_labels=["INBOX"])


def mark_read(email: str, message_id: str):
    return modify_labels(email, message_id, remove_labels=["UNREAD"])
