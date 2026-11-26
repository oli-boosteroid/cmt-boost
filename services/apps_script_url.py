"""
Only real Google Apps Script web-app URLs may receive the bot's sheet secrets.

The web-app URL of each sheet integration can be set from the console admin page (console_config), and
the bot then sends its shared secret and data there. A wrong or hostile value would hand that secret to
another server, or make the bot call an address inside the company network. So every URL is checked
against the shapes Google uses for deployed web apps; anything else is treated as "not configured".
"""
import logging
import re

logger = logging.getLogger(__name__)

_ALLOWED = re.compile(
    r"^https://script\.google\.com/(?:a/macros/[A-Za-z0-9.-]+|macros)/s/[A-Za-z0-9_-]{10,200}/(?:exec|dev)$")
_warned = set()


def safe_webapp_url(url: str, name: str = "") -> str:
    """The URL if it is a Google Apps Script web-app address, else "" (logged once per bad value)."""
    url = (url or "").strip()
    if not url:
        return ""
    if _ALLOWED.match(url):
        return url
    key = (name, url)
    if key not in _warned:
        _warned.add(key)
        logger.error("[AppsScript] %s web-app URL refused: it is not a script.google.com/macros/s/.../exec "
                     "address", name or "sheet")
    return ""
