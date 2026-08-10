"""Gmail + Calendar OAuth, connected conversationally (no slash commands).

Flow:
1. User asks to connect Gmail/Calendar -> connect_google tool -> we generate
   a per-user random `state`, store it on the User row, and return an
   authorization URL for them to open in a browser.
2. Google redirects to GOOGLE_OAUTH_REDIRECT_URI (a tiny aiohttp server we
   run inside the bot process — see start_oauth_server) with ?code=...&state=...
3. The callback server exchanges the code for tokens and saves them on the
   User row matched by state, then messages the user on Telegram to confirm.

Note: the redirect URI must be reachable by whichever browser the user opens
it in, so for a bot with multiple real users, GOOGLE_OAUTH_REDIRECT_URI needs
to point at a publicly reachable host (not localhost) with GOOGLE_OAUTH_PORT
open. For local/dev/demo use, localhost is fine since the developer opens the
link on the same machine the bot runs on.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone

from aiohttp import web
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

import config
from database import get_session
from models import User

logger = logging.getLogger(__name__)

CLIENT_CONFIG = {
    "web": {
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [config.GOOGLE_OAUTH_REDIRECT_URI],
    }
}


def google_configured() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


def _build_flow() -> Flow:
    flow = Flow.from_client_config(CLIENT_CONFIG, scopes=config.GOOGLE_SCOPES)
    flow.redirect_uri = config.GOOGLE_OAUTH_REDIRECT_URI
    return flow


def build_auth_url(session, user: User) -> str:
    """Generate a fresh state for this user and return the Google consent URL."""
    state = secrets.token_urlsafe(24)
    user.oauth_state = state
    session.commit()

    flow = _build_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # ensures a refresh_token is returned even on repeat connects
        state=state,
    )
    return auth_url


def is_connected(user: User) -> bool:
    return bool(user.google_refresh_token)


def _credentials_from_user(user: User) -> Credentials:
    return Credentials(
        token=user.google_access_token,
        refresh_token=user.google_refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        scopes=config.GOOGLE_SCOPES,
    )


def get_credentials(session, user: User) -> Credentials | None:
    """Return valid, auto-refreshed credentials for this user, or None if
    they haven't connected Google yet."""
    if not is_connected(user):
        return None

    creds = _credentials_from_user(user)

    expiry_stale = True
    if user.google_token_expiry:
        try:
            expiry = datetime.fromisoformat(user.google_token_expiry)
            expiry_stale = datetime.now(timezone.utc) >= expiry
        except ValueError:
            expiry_stale = True

    if expiry_stale:
        creds.refresh(GoogleRequest())
        user.google_access_token = creds.token
        if creds.expiry:
            user.google_token_expiry = creds.expiry.replace(tzinfo=timezone.utc).isoformat()
        session.commit()

    return creds


# ---------------------------------------------------------------------------
# OAuth callback server — runs inside the bot process
# ---------------------------------------------------------------------------

async def _oauth_callback(request: web.Request) -> web.Response:
    application = request.app["telegram_application"]
    code = request.query.get("code")
    state = request.query.get("state")
    error = request.query.get("error")

    if error or not code or not state:
        return web.Response(
            text="Google authorization failed or was cancelled. You can close this tab "
                 "and try again from the Telegram chat.",
            status=400,
        )

    session = get_session()
    try:
        user = session.query(User).filter(User.oauth_state == state).first()
        if not user:
            return web.Response(
                text="This authorization link expired or was already used. "
                     "Ask the bot to reconnect Google and try again.",
                status=400,
            )

        try:
            flow = _build_flow()
            flow.fetch_token(code=code)
            creds = flow.credentials
        except Exception as exc:
            logger.error("Google token exchange failed for user %s: %s", user.id, exc)
            return web.Response(
                text="Something went wrong finishing the Google connection. "
                     "Please try again from Telegram.",
                status=500,
            )

        user.google_access_token = creds.token
        user.google_refresh_token = creds.refresh_token or user.google_refresh_token
        if creds.expiry:
            user.google_token_expiry = creds.expiry.replace(tzinfo=timezone.utc).isoformat()
        user.oauth_state = None
        session.commit()

        try:
            await application.bot.send_message(
                chat_id=user.telegram_id,
                text="Gmail and Calendar are connected! I can now pull recent emails "
                     "and upcoming meetings into our conversation whenever it's useful.",
            )
        except Exception as exc:
            logger.warning("Couldn't notify user %s after Google connect: %s", user.id, exc)

    finally:
        session.close()

    return web.Response(text="Google account connected — you can close this tab and go back to Telegram.")


def start_oauth_server(application) -> None:
    """Start the small callback server as a background asyncio task.
    Call this once, after the bot's event loop is running (e.g. from a
    post_init hook), not before."""
    if not google_configured():
        logger.info("Google OAuth not configured — skipping callback server.")
        return

    app = web.Application()
    app["telegram_application"] = application
    app.router.add_get("/oauth2callback", _oauth_callback)

    runner = web.AppRunner(app)

    async def _run():
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", config.GOOGLE_OAUTH_PORT)
        await site.start()
        logger.info("Google OAuth callback server listening on :%d", config.GOOGLE_OAUTH_PORT)

    import asyncio
    asyncio.ensure_future(_run())
