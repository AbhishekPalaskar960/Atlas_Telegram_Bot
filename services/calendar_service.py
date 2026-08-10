import logging
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from models import User
from services import google_auth_service

logger = logging.getLogger(__name__)

MAX_RESULTS = 10


def get_upcoming_events(session, user: User, days_ahead: int = 7) -> str:
    """List the user's upcoming Calendar events over the next N days — used for
    daily briefings and meeting prep ('what's on my calendar today/tomorrow')."""
    if not google_auth_service.google_configured():
        return "ERROR: Calendar isn't configured on this bot yet (missing GOOGLE_CLIENT_ID/SECRET)."
    if not google_auth_service.is_connected(user):
        return "ERROR: Calendar isn't connected for this user. Use the connect_google tool first."

    creds = google_auth_service.get_credentials(session, user)
    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        now = datetime.now(timezone.utc)
        time_max = now + timedelta(days=max(1, min(days_ahead, 30)))

        result = service.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=time_max.isoformat(),
            maxResults=MAX_RESULTS,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = result.get("items", [])
        if not events:
            return f"No events on the calendar in the next {days_ahead} day(s)."

        lines = []
        for event in events:
            start = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
            title = event.get("summary", "(no title)")
            attendees = event.get("attendees", [])
            attendee_names = ", ".join(a.get("email", "") for a in attendees[:5])
            line = f"- {start}: {title}"
            if attendee_names:
                line += f" (with {attendee_names})"
            lines.append(line)

        return "\n".join(lines)
    except HttpError as exc:
        logger.warning("Calendar API error for user %s: %s", user.id, exc)
        return "ERROR: couldn't reach Google Calendar right now. The connection may need to be refreshed."
