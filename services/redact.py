"""
Never show users' email addresses in Slack or the console.

Users often paste their account email into ModMail / posts / comments. The bot still reads the raw text
to look the User ID up (services.mongo_db_service.ident_status), but everything it shows or stores for
display goes through redact_emails(): each address becomes "[email hidden]", followed by
"(User ID <id>)" when the user's ID is known.
"""
import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HIDDEN = "[email hidden]"


def user_id_from(value) -> str:
    """'User ID 7648744' (how the bot stores it) -> '7648744'; anything else -> ''."""
    s = str(value or "").strip()
    return s[len("User ID"):].strip() if s.startswith("User ID") else ""


def _known_user_id(email: str) -> str:
    try:
        from services.user_ref import known_user_id
        return known_user_id(email) or ""
    except Exception:
        return ""


def mask_email(value) -> str:
    """Mask an email for logs: 'john.doe@example.com' -> 'j***@example.com'. A non-email (e.g. a session
    id) becomes '***'; empty stays ''."""
    v = (value or "").strip()
    if not v:
        return ""
    if "@" not in v:
        return "***"
    local, _, domain = v.partition("@")
    return (local[:1] if local else "") + "***@" + domain


def redact_emails(text, user_id: str = "") -> str:
    """Each address -> "[email hidden]", plus "(User ID n)" when that address's User ID is known (given,
    or already looked up in this process - no cloud request here)."""
    if not text:
        return text or ""

    def sub(m):
        uid = user_id or _known_user_id(m.group(0))
        if uid:
            return HIDDEN + f" (User ID {uid})"
        return HIDDEN

    return EMAIL_RE.sub(sub, str(text))


ADMIN_SESSIONS_URL = ("https://admin.cloud.boosteroid.com/admin/filament/streaming/"
                      "streaming-user-sessions?filters%5Buser_id%5D%5Bvalue%5D=")
_UID_RE = re.compile(r"(?<![|/=\w])User ID (\d{3,12})\b")


def link_user_ids_slack(text) -> str:
    """'User ID 7648744' -> a Slack link to that user's sessions in the cloud admin."""
    if not text:
        return text or ""
    return _UID_RE.sub(lambda m: f"<{ADMIN_SESSIONS_URL}{m.group(1)}|User ID {m.group(1)}>", str(text))
