import base64
import logging

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from models import User
from services import google_auth_service

logger = logging.getLogger(__name__)

MAX_RESULTS = 8


def _decode_snippet(payload: dict) -> str:
    """Best-effort plain-text body extraction; falls back to Gmail's snippet."""
    def _walk(part):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", "replace")
        for sub in part.get("parts", []) or []:
            found = _walk(sub)
            if found:
                return found
        return None

    body = _walk(payload) or ""
    return body.strip()[:800]


def search_recent_emails(session, user: User, query: str = "", max_results: int = MAX_RESULTS) -> str:
    """List recent Gmail messages, optionally filtered by a Gmail search query
    (e.g. 'from:acme.com', 'subject:earnings', 'newer_than:3d')."""
    if not google_auth_service.google_configured():
        return "ERROR: Gmail isn't configured on this bot yet (missing GOOGLE_CLIENT_ID/SECRET)."
    if not google_auth_service.is_connected(user):
        return "ERROR: Gmail isn't connected for this user. Use the connect_google tool first."

    creds = google_auth_service.get_credentials(session, user)
    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        result = service.users().messages().list(
            userId="me", q=query or "", maxResults=max_results
        ).execute()
        msg_refs = result.get("messages", [])
        if not msg_refs:
            return "No emails matched that." if query else "Inbox looks quiet — no recent emails."

        summaries = []
        for ref in msg_refs:
            full = service.users().messages().get(
                userId="me", id=ref["id"], format="full"
            ).execute()
            headers = {
                h["name"]: h["value"]
                for h in full.get("payload", {}).get("headers", [])
            }
            subject = headers.get("Subject", "(no subject)")
            sender = headers.get("From", "unknown sender")
            date = headers.get("Date", "")
            snippet = full.get("snippet") or _decode_snippet(full.get("payload", {}))
            summaries.append(f"- From: {sender} | Subject: {subject} | {date}\n  {snippet}")

        return "\n".join(summaries)
    except HttpError as exc:
        logger.warning("Gmail API error for user %s: %s", user.id, exc)
        return "ERROR: couldn't reach Gmail right now. The connection may need to be refreshed."
