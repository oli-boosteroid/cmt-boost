"""
Internal Jira gateway for the CMT Console (console on the same VM).

The console holds no Jira credentials. It forwards each Jira call here (cmt-console/services/jira_http.py);
the bot adds its own Jira credentials and passes the call on, but only:
  - from the Docker network: this server listens on JIRA_GATEWAY_PORT (default 7001) inside the container
    and the port is NOT published in docker-compose.yml;
  - with the shared JIRA_GATEWAY_TOKEN (no token configured = gateway off);
  - for the Jira paths the console actually uses (issue / comments / transitions / attachments / edit meta,
    search, myself, attachment content) and methods GET / POST / PUT (no DELETE);
  - never following redirects (the credentials could otherwise leave the Jira host).
Every call is logged with the console user who asked for it.
"""
import hmac
import logging
import os
import re
import threading
from base64 import b64encode

import requests
from flask import Flask, Response, request

logger = logging.getLogger(__name__)

_ALLOWED_PATH = re.compile(
    r"^/(?:rest/api/[23]/(?:issue/[A-Z][A-Z0-9]*-\d+(?:/(?:comment(?:/\d+)?|transitions|editmeta|attachments))?"
    r"|issue|search/jql|search|myself|attachment/content/\d+|attachment/thumbnail/\d+)"
    r"|secure/attachment/\d+/[^?#]*|secure/thumbnail/\d+/[^?#]*)(?:\?.*)?$")
_METHODS = {"GET", "POST", "PUT"}
_MAX_BODY = 55 * 1024 * 1024

app = Flask("jira_gateway")
app.config["MAX_CONTENT_LENGTH"] = _MAX_BODY


def _token() -> str:
    return (os.getenv("JIRA_GATEWAY_TOKEN") or "").strip()


def allowed(method: str, path: str) -> bool:
    return method in _METHODS and bool(_ALLOWED_PATH.match(path or "")) and ".." not in (path or "")


@app.route("/internal/jira", methods=["POST"])
def forward():
    expected = _token()
    if not expected or not hmac.compare_digest(request.headers.get("X-Gateway-Token", ""), expected):
        return Response("forbidden", status=403)
    method = (request.headers.get("X-Jira-Method") or "").upper()
    path = request.headers.get("X-Jira-Path") or ""
    who = (request.headers.get("X-Console-User") or "")[:120]
    if not allowed(method, path):
        logger.warning("[JiraGateway] refused %s %s (console user %s)", method, path[:200], who or "?")
        return Response("path not allowed", status=403)
    base = (os.getenv("JIRA_BASE_URL") or "").rstrip("/")
    email, api_token = os.getenv("JIRA_API_EMAIL", ""), os.getenv("JIRA_API_TOKEN", "")
    if not (base and email and api_token):
        return Response("jira not configured on the bot", status=503)
    headers = {"Authorization": "Basic " + b64encode((email + ":" + api_token).encode()).decode()}
    for h in ("Content-Type", "Accept", "X-Atlassian-Token"):
        v = request.headers.get("X-Jira-" + h)
        if v:
            headers[h] = v
    try:
        r = requests.request(method, base + path, data=request.get_data() if method != "GET" else None,
                             headers=headers, timeout=60, allow_redirects=False)
    except requests.RequestException as e:
        logger.error("[JiraGateway] %s %s failed: %s", method, path[:200], type(e).__name__)
        return Response("jira unreachable", status=502)
    logger.info("[JiraGateway] %s %s -> %s (console user %s)", method, path.split("?")[0][:200], r.status_code, who or "?")
    return Response(r.content, status=r.status_code,
                    content_type=r.headers.get("Content-Type", "application/octet-stream"))


def start():
    """Serve the gateway on its own internal port (thread). Off unless JIRA_GATEWAY_TOKEN is set."""
    if not _token():
        logger.info("[JiraGateway] off (JIRA_GATEWAY_TOKEN not set)")
        return
    port = int(os.getenv("JIRA_GATEWAY_PORT", "7001"))

    def run():
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=8, connection_limit=100, channel_timeout=90)

    threading.Thread(target=run, daemon=True, name="jira-gateway").start()
    logger.info("[JiraGateway] listening on internal port %s (not published)", port)
