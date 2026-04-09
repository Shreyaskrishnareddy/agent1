"""Gmail API client for OTP codes and verification links.

Handles email verification during job site account creation:
- Polls Gmail for recent verification emails
- Extracts OTP codes (4-8 digit numbers)
- Extracts verification/confirmation links
- Supports IMAP fallback for non-Gmail providers

Setup: Run `agent1 gmail-setup` to authenticate via OAuth.
"""

import base64
import logging
import os
import re
import time
from pathlib import Path

from agent1 import config

logger = logging.getLogger(__name__)

# Paths for Gmail credentials
CREDENTIALS_PATH = config.APP_DIR / "gmail_credentials.json"
TOKEN_PATH = config.APP_DIR / "gmail_token.json"

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Common OTP patterns
_OTP_PATTERNS = [
    r'\b(\d{6})\b',          # 6-digit (most common)
    r'\b(\d{4})\b',          # 4-digit
    r'\b(\d{8})\b',          # 8-digit
    r'code[:\s]+(\d{4,8})',  # "code: 123456"
    r'pin[:\s]+(\d{4,8})',   # "pin: 1234"
    r'otp[:\s]+(\d{4,8})',   # "otp: 123456"
]

# Verification link patterns
_VERIFY_LINK_PATTERNS = [
    r'(https?://\S*verif\S*)',
    r'(https?://\S*confirm\S*)',
    r'(https?://\S*activate\S*)',
    r'(https?://\S*validate\S*)',
    r'(https?://\S*auth\S*token\S*)',
]


class GmailClient:
    """Gmail API client for reading verification emails."""

    def __init__(self):
        self._service = None

    def is_configured(self) -> bool:
        """Check if Gmail credentials are set up."""
        return CREDENTIALS_PATH.exists()

    def authenticate(self) -> bool:
        """Run OAuth flow to get Gmail read access.

        Opens a browser for the user to grant permission.
        Stores token at ~/.agent1/gmail_token.json.

        Returns True if successful.
        """
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError:
            logger.error("Gmail API packages not installed")
            return False

        if not CREDENTIALS_PATH.exists():
            logger.error(
                "Gmail credentials not found at %s. "
                "Download OAuth credentials from Google Cloud Console.",
                CREDENTIALS_PATH,
            )
            return False

        creds = None

        # Load existing token
        if TOKEN_PATH.exists():
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

        # Refresh or get new token
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(CREDENTIALS_PATH), SCOPES
                )
                creds = flow.run_local_server(port=0)

            TOKEN_PATH.write_text(creds.to_json())

        from googleapiclient.discovery import build
        self._service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail authenticated successfully")
        return True

    def _get_service(self):
        """Get or create the Gmail service."""
        if self._service is not None:
            return self._service

        if not TOKEN_PATH.exists():
            raise RuntimeError(
                "Gmail not authenticated. Run `agent1 gmail-setup` first."
            )

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    def search_emails(
        self,
        query: str = "",
        max_results: int = 5,
        max_age_minutes: int = 5,
    ) -> list[dict]:
        """Search Gmail for recent emails.

        Args:
            query: Gmail search query (e.g. "from:noreply@company.com").
            max_results: Max emails to return.
            max_age_minutes: Only return emails newer than this.

        Returns:
            List of dicts with keys: id, subject, from, body, date.
        """
        service = self._get_service()

        full_query = f"newer_than:{max_age_minutes}m"
        if query:
            full_query = f"{query} {full_query}"

        try:
            results = service.users().messages().list(
                userId="me",
                q=full_query,
                maxResults=max_results,
            ).execute()
        except Exception as e:
            logger.error("Gmail search failed: %s", e)
            return []

        messages = results.get("messages", [])
        emails = []

        for msg_ref in messages:
            try:
                msg = service.users().messages().get(
                    userId="me",
                    id=msg_ref["id"],
                    format="full",
                ).execute()
                emails.append(self._parse_message(msg))
            except Exception as e:
                logger.debug("Failed to read message %s: %s", msg_ref["id"], e)

        return emails

    def _parse_message(self, msg: dict) -> dict:
        """Parse a Gmail API message into a simple dict."""
        headers = msg.get("payload", {}).get("headers", [])

        result = {"id": msg["id"], "subject": "", "from": "", "date": "", "body": ""}

        for h in headers:
            name = h["name"].lower()
            if name == "subject":
                result["subject"] = h["value"]
            elif name == "from":
                result["from"] = h["value"]
            elif name == "date":
                result["date"] = h["value"]

        # Extract body
        result["body"] = self._extract_body(msg.get("payload", {}))
        return result

    def _extract_body(self, payload: dict) -> str:
        """Extract plain text body from Gmail message payload."""
        # Simple message
        body_data = payload.get("body", {}).get("data", "")
        if body_data:
            return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

        # Multipart message
        parts = payload.get("parts", [])
        for part in parts:
            mime = part.get("mimeType", "")
            if mime == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        # Fallback: try text/html
        for part in parts:
            mime = part.get("mimeType", "")
            if mime == "text/html":
                data = part.get("body", {}).get("data", "")
                if data:
                    html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    # Strip HTML tags for basic text extraction
                    return re.sub(r'<[^>]+>', ' ', html).strip()

        # Nested multipart
        for part in parts:
            if part.get("parts"):
                result = self._extract_body(part)
                if result:
                    return result

        return ""

    def get_verification_code(
        self,
        sender_hint: str = "",
        subject_hint: str = "",
        timeout: int = 60,
        poll_interval: int = 5,
    ) -> str | None:
        """Poll Gmail for a verification code.

        Args:
            sender_hint: Partial sender email/name to filter by.
            subject_hint: Partial subject to filter by.
            timeout: Max seconds to wait.
            poll_interval: Seconds between polls.

        Returns:
            The OTP code string, or None if not found.
        """
        query_parts = ["is:unread"]
        if sender_hint:
            query_parts.append(f"from:{sender_hint}")
        if subject_hint:
            query_parts.append(f"subject:{subject_hint}")
        query = " ".join(query_parts)

        deadline = time.time() + timeout

        while time.time() < deadline:
            emails = self.search_emails(query=query, max_results=3, max_age_minutes=5)

            for email in emails:
                body = email.get("body", "")
                subject = email.get("subject", "")
                text = f"{subject} {body}"

                # Try each OTP pattern
                for pattern in _OTP_PATTERNS:
                    match = re.search(pattern, text, re.IGNORECASE)
                    if match:
                        code = match.group(1)
                        logger.info("Found OTP code: %s (from: %s)", code, email.get("from", ""))
                        return code

            time.sleep(poll_interval)

        logger.warning("No verification code found after %ds", timeout)
        return None

    def get_verification_link(
        self,
        sender_hint: str = "",
        subject_hint: str = "",
        timeout: int = 60,
        poll_interval: int = 5,
    ) -> str | None:
        """Poll Gmail for a verification link.

        Args:
            sender_hint: Partial sender to filter by.
            subject_hint: Partial subject to filter by.
            timeout: Max seconds to wait.
            poll_interval: Seconds between polls.

        Returns:
            The verification URL string, or None if not found.
        """
        query_parts = ["is:unread"]
        if sender_hint:
            query_parts.append(f"from:{sender_hint}")
        if subject_hint:
            query_parts.append(f"subject:({subject_hint})")
        query = " ".join(query_parts)

        deadline = time.time() + timeout

        while time.time() < deadline:
            emails = self.search_emails(query=query, max_results=3, max_age_minutes=5)

            for email in emails:
                body = email.get("body", "")

                for pattern in _VERIFY_LINK_PATTERNS:
                    match = re.search(pattern, body)
                    if match:
                        link = match.group(1).rstrip(".,;>)")
                        logger.info("Found verification link: %s", link[:80])
                        return link

            time.sleep(poll_interval)

        logger.warning("No verification link found after %ds", timeout)
        return None


# ---------------------------------------------------------------------------
# IMAP fallback (for non-Gmail providers)
# ---------------------------------------------------------------------------

class IMAPClient:
    """IMAP email client for non-Gmail providers."""

    def __init__(self, host: str, username: str, password: str):
        self.host = host
        self.username = username
        self.password = password

    def get_verification_code(self, timeout: int = 60, poll_interval: int = 5) -> str | None:
        """Poll IMAP inbox for a verification code."""
        import imaplib
        import email as email_lib

        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                mail = imaplib.IMAP4_SSL(self.host)
                mail.login(self.username, self.password)
                mail.select("inbox")

                _, data = mail.search(None, "UNSEEN")
                for msg_id in reversed(data[0].split()):
                    _, msg_data = mail.fetch(msg_id, "(RFC822)")
                    msg = email_lib.message_from_bytes(msg_data[0][1])

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                                break
                    else:
                        body = msg.get_payload(decode=True).decode("utf-8", errors="replace")

                    subject = msg.get("Subject", "")
                    text = f"{subject} {body}"

                    for pattern in _OTP_PATTERNS:
                        match = re.search(pattern, text, re.IGNORECASE)
                        if match:
                            mail.logout()
                            return match.group(1)

                mail.logout()
            except Exception as e:
                logger.debug("IMAP error: %s", e)

            time.sleep(poll_interval)

        return None


# Singleton
_gmail_client: GmailClient | None = None


def get_gmail_client() -> GmailClient:
    """Get the singleton Gmail client."""
    global _gmail_client
    if _gmail_client is None:
        _gmail_client = GmailClient()
    return _gmail_client
