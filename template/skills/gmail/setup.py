#!/usr/bin/env python3
"""
Run once per Gmail account to complete OAuth flow and store tokens in macOS Keychain.
Usage: python3 setup.py you@example.com
       python3 setup.py assistant@example.com
"""

import json
import sys
import os
import keyring
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "../../secrets/credentials.json")
KEYRING_SERVICE = "core-gmail"


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 setup.py <email>")
        sys.exit(1)

    email = sys.argv[1]
    creds_path = os.path.abspath(CREDENTIALS_FILE)

    if not os.path.exists(creds_path):
        print(f"credentials.json not found at: {creds_path}")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
    creds = flow.run_local_server(port=0)

    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes),
    }

    keyring.set_password(KEYRING_SERVICE, email, json.dumps(token_data))
    print(f"Token stored in Keychain for {email}")


if __name__ == "__main__":
    main()
