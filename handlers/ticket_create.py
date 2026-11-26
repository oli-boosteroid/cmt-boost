"""
The one place that builds SO and URG Jira tickets. Both the CMT Console (through the action queue)
and the Slack "Create Jira Ticket" / "Create Urgent Request" modals call the functions here.

The console collects the form values in the SAME shape as a Slack `view["state"]["values"]`
(block_id -> action_id -> {"value" | "selected_option" | "selected_options" | "files"}) plus the
files, enqueues a console_action, and the bot worker (services/console_actions_worker.py) calls the
functions here. Files arrive already decoded as {"name", "mimetype", "content" (bytes)}.

Slack (handlers/modal_handlers.py) downloads its file_input files into the same shape and calls
create_so_ticket / create_urgent_ticket, so there is no second copy of this logic.
This runs inside the bot process, so it may use the bot-only jira/mongo/reddit helpers.
"""
import os
import re
import logging

logger = logging.getLogger(__name__)


# ── Author mention (identical to both Slack handlers) ────────────────────────────────────────────
def _author_mention(author_email, author_display):
    from services.jira_service import find_jira_user_by_email
    account_id = None
    if author_email:
        try:
            from services.identity_service import get_jira_our_user_map
            alias = author_email.split('@')[0]
            alias_to_id = {v: k for k, v in get_jira_our_user_map().items()}
            account_id = alias_to_id.get(alias)
        except Exception:
            account_id = None
    if not account_id and author_email:
        account_id = find_jira_user_by_email(author_email)
    if account_id:
        return f"[~accountId:{account_id}]"
    if author_email:
        return author_email
    return f"@{author_display or 'CM'}"


_NO_ACCOUNT_MSG = ("No Boosteroid account was found for this email. Tickets identify the user by User ID: "
                   "check the address or enter the User ID instead.")


_LOOKUP_DOWN_MSG = ("The User ID could not be looked up (the cloud admin did not answer). "
                    "Enter the User ID by hand instead of the email and create the ticket again.")


def _ticket_user_id(text, values, comm_link=""):
    """Personal-data rule for tickets: an email typed into the form is never written to Jira.

    Returns (text, error). Every address in `text` is replaced with "User ID N" (resolved through the
    cloud API) and that ID is saved to the directory for this user. If an address cannot be resolved the
    ticket is refused with `error`, so nothing with an email in it is created. A bare number in an email
    field is taken as the User ID itself. Without an API key the text is returned unchanged."""
    text = (text or "").strip()
    if not text:
        return text, None
    from services.user_ref import EMAIL_RE, resolve_email, remember_user_id, ref_from_link
    ref = dict((values or {}).get("_user_ref") or {}) or ref_from_link(comm_link)
    failed = []

    def sub(m):
        uid, status = resolve_email(m.group(0))      # our own directory first, the cloud API only if needed
        if not uid:
            failed.append(status)
            return m.group(0)
        remember_user_id(uid, email=m.group(0), ref=ref)
        return "User ID %s" % uid

    out = EMAIL_RE.sub(sub, text)
    if failed:
        return text, (_LOOKUP_DOWN_MSG if "error" in failed else _NO_ACCOUNT_MSG)
    um = re.search(r"user\s*id\s*:?\s*(\d{1,12})", out, re.IGNORECASE) or re.fullmatch(r"\s*(\d{3,12})\s*", out)
    if um and ref:
        remember_user_id(um.group(1), ref=ref)
    return out, None


def _admin_user_id_link(uid):
    """Jira link "User ID N" -> the user's streaming sessions in the cloud admin, filtered by ID."""
    uid = int(uid)
    url = ("https://admin.cloud.boosteroid.com/admin/filament/streaming/"
           "streaming-user-sessions?filters%5Buser_id%5D%5Bvalue%5D=" + str(uid))
    return f"[User ID {uid}|{url}]"


def _admin_email_link(email):
    _plain = re.fullmatch(r"\s*(?:user\s*id\s*:?\s*)?(\d{3,12})\s*", str(email or ""), re.IGNORECASE)
    if _plain:                       # the field already holds a User ID
        return _admin_user_id_link(_plain.group(1))
    # Personal-data rule: a ticket names the user by User ID, not by email. When the cloud API can
    # resolve the address, the ticket gets "User ID N" and the email is not written at all; only if it
    # cannot (no API key yet, unknown address, API down) does the old linked email remain.
    try:
        from services.user_ref import user_id_for_email
        uid = user_id_for_email(email)
        if uid:
            return _admin_user_id_link(uid)
    except Exception as e:
        logger.warning("[TicketCreate] User ID lookup skipped: %s", type(e).__name__)
    return "User ID unknown"      # an address is never written into a ticket


def _dedup_files(file_list):
    """Deduplicate names globally (clipboard files often share 'image.png') -> [(file, unique_name)]."""
    seen, out = {}, []
    for f in file_list or []:
        name = f.get("name", "attachment")
        if name in seen:
            seen[name] += 1
            if "." in name:
                base, ext = name.rsplit(".", 1)
                unique = f"{base}_{seen[name]}.{ext}"
            else:
                unique = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
            unique = name
        out.append((f, unique))
    return out


def _v(values, block, action):
    return (values.get(block, {}).get(action, {}).get("value") or "").strip()


def _s(values, block, action):
    sel = values.get(block, {}).get(action, {}).get("selected_option")
    return sel["value"] if sel else ""


# ── SO board (Create Jira Ticket) ────────────────────────────────────────────────────────────────
_SO_FIELD_ORDER = [
    ("email_block", "email_input", "Email", False),
    ("app_id_playground_block", "app_id_playground_input", "ID / Application / Playground", False),
    ("uniq_id_block", "uniq_id_input", "Uniq ID", False),
    ("connection_block", "connection_input", "Connection type", True),
    ("multiple_games_block", "multiple_games_input", "Appears in multiple games", True),
    ("other_devices_block", "other_devices_input", "Occurs on multiple devices/networks", True),
    ("issue_origin_block", "issue_origin_input", "Issue origin", True),
    ("third_party_block", "third_party_input", "Additional network/system load on user's end", True),
    ("dlc_name_block", "dlc_name_input", "DLC name/link", False),
    ("local_request_type_block", "local_request_type_input", "Local request type", True),
    ("saves_block", "saves_input", "Save type", True),
    ("account_type_block", "account_type_input", "Account type", True),
    ("gamepad_model_block", "gamepad_model_input", "Gamepad model", False),
    ("gamepad_connection_block", "gamepad_connection_input", "Connection type", True),
]
_SO_FILE_BLOCKS = {
    "Poor quality/lags": [("speedtest_block", "speedtest_input"), ("mtr_block", "mtr_input"),
                          ("mtu_block", "mtu_input"), ("screenshots_block", "screenshots_input")],
    "Game issue": [("media_block", "media_input")],
    "Launcher issue": [("media_block", "media_input")],
    "Desktop application issue": [("media_block", "media_input"), ("log_block", "log_input")],
    "TV application issue": [("media_block", "media_input"), ("tv_os_block", "tv_os_input")],
    "Saves issue": [("steam_remote_block", "steam_remote_input"), ("screenshots_block", "screenshots_input")],
    "DLC request": [("dlc_block", "dlc_input")],
    "Gamepad issue": [("media_block", "media_input"), ("gamepad_tester_block", "gamepad_tester_input")],
    "Site issue": [("media_block", "media_input"), ("console_block", "console_input")],
    "Other": [("media_block", "media_input")],
}
_SO_FILE_BLOCK_LABELS = {
    "speedtest_block": "Speedtest", "mtr_block": "MTR", "mtu_block": "MTU",
    "screenshots_block": "Screenshot/video", "media_block": "Screenshot/video",
    "tv_os_block": "TV OS version", "steam_remote_block": "Steam Remote Storage",
    "dlc_block": "DLCs screenshot", "gamepad_tester_block": "Gamepad Tester", "console_block": "Browser console",
}


def create_so_ticket(topic, values, author_email, author_display, files_by_block=None):
    """Create a SO Jira ticket exactly like handle_create_ticket_submit. `files_by_block` maps a
    file block_id -> [ {"name","mimetype","content"} ]. Returns {"ok","key","summary","error"}."""
    from services.jira_service import create_jira_issue, attach_file_to_jira, L1_TAG_NAME_TO_ENUM, set_issue_communication_link
    files_by_block = files_by_block or {}
    author_mention = _author_mention(author_email, author_display)

    _l1_names = [o["value"] for o in values.get("l1_tags_block", {}).get("l1_tags_input", {}).get("selected_options", [])]
    l1_tags = [L1_TAG_NAME_TO_ENUM[n] for n in _l1_names if n in L1_TAG_NAME_TO_ENUM]

    # The Email field is turned into a User ID first; an address with no account refuses the ticket.
    _raw_email = _v(values, "email_block", "email_input")
    if _raw_email:
        _resolved, _err = _ticket_user_id(_raw_email, values, _v(values, "comm_link_block", "comm_link_input"))
        if _err:
            return {"ok": False, "error": _err}
        values = dict(values)
        values["email_block"] = {"email_input": {"value": re.sub(r"(?i)^user id\s*", "", _resolved).strip()}}
    desc_lines = [f"*Author:* {author_mention}", ""]
    for block_id, action_id, label, is_select in _SO_FIELD_ORDER:
        if block_id not in values:
            continue
        val = _s(values, block_id, action_id) if is_select else _v(values, block_id, action_id)
        if not val:
            continue
        if block_id == "email_block":
            desc_lines.append(_admin_email_link(val))
            continue
        if block_id == "app_id_playground_block":
            first = True
            for raw in val.splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                if raw.lower().startswith("id:"):
                    if not first:
                        desc_lines.append("")
                    first = False
                if ":" in raw:
                    prefix, rest = raw.split(":", 1)
                    desc_lines.append(f"*{prefix}:*{rest}")
                else:
                    desc_lines.append(raw)
            desc_lines.append("")
            continue
        desc_lines.append(f"*{label}:* {val}")

    description_text = _v(values, "description_block", "description_input")
    steps_text = _v(values, "steps_block", "steps_input")
    if steps_text:
        desc_lines += ["", "*Steps already taken:*", steps_text]
    if description_text:
        desc_lines += ["", "*Description:*", description_text]
    comm_link = _v(values, "comm_link_block", "comm_link_input")

    # Summary (topic-specific), identical to the Slack handler
    ticket_name = _v(values, "ticket_name_block", "ticket_name_input")
    email_val = _v(values, "email_block", "email_input")
    if topic in ("Desktop application issue", "TV application issue") and ticket_name:
        app_val = _s(values, "application_block", "application_input")
        problem = ticket_name.split(" - ", 1)[1].strip() if " - " in ticket_name else ticket_name
        summary = f"{app_val} - {problem}"
    elif topic == "Launcher issue" and ticket_name:
        launcher_val = _s(values, "launcher_block", "launcher_input")
        problem = ticket_name.split(" - ", 1)[1].strip() if " - " in ticket_name else ticket_name
        summary = f"{launcher_val} - {problem}"
    elif topic == "DLC request" and ticket_name:
        game_name = ticket_name
        if game_name.lower().endswith(" - dlc request"):
            game_name = game_name[:-len(" - dlc request")].strip()
        summary = f"{game_name} - DLC request"
    elif topic == "Local saves/config support request":
        session_raw = _v(values, "app_id_playground_block", "app_id_playground_input")
        app_name = ""
        for line in session_raw.splitlines():
            line = line.strip()
            if line.lower().startswith("application:"):
                app_name = re.sub(r'\s*\[ID:\s*\d+\]', '', line[12:]).strip()
                break
        request_type = _s(values, "local_request_type_block", "local_request_type_input")
        suffix = {"Config": "config request", "Local saves": "local saves request",
                  "Local saves & config": "local saves & config request"}.get(request_type, request_type.lower())
        summary = f"{app_name} - {suffix}" if app_name else suffix
    elif topic in ("Game issue", "Saves issue") and ticket_name:
        session_raw = _v(values, "app_id_playground_block", "app_id_playground_input")
        app_name = ""
        for line in session_raw.splitlines():
            line = line.strip()
            if line.lower().startswith("application:"):
                app_name = re.sub(r'\s*\[ID:\s*\d+\]', '', line[12:]).strip()
                break
        problem = ticket_name.split(" - ", 1)[1].strip() if " - " in ticket_name else ticket_name
        summary = f"{app_name} - {problem}" if app_name else problem
    elif ticket_name:
        summary = ticket_name
    elif email_val:
        _who_label = ("User ID " + email_val) if email_val.isdigit() else email_val
        summary = f"{topic} — {_who_label}"
    elif description_text:
        summary = f"{topic} — {description_text[:60]}"
    else:
        summary = topic

    # Collect files (dedup names globally across this topic's blocks) + inline image refs
    seen, all_files = {}, []   # all_files: (block_id, file, unique_name)
    for bid, aid in _SO_FILE_BLOCKS.get(topic, []):
        for f in files_by_block.get(bid, []):
            name = f.get("name", "attachment")
            if name in seen:
                seen[name] += 1
                if "." in name:
                    base, ext = name.rsplit(".", 1)
                    unique = f"{base}_{seen[name]}.{ext}"
                else:
                    unique = f"{name}_{seen[name]}"
            else:
                seen[name] = 0
                unique = name
            all_files.append((bid, f, unique))
    for bid, aid in _SO_FILE_BLOCKS.get(topic, []):
        imgs = [(u, f) for (b, f, u) in all_files if b == bid and (f.get("mimetype", "") or "").startswith("image/")]
        if imgs:
            desc_lines.append("")
            lbl = _SO_FILE_BLOCK_LABELS.get(bid, "")
            if lbl:
                desc_lines.append(f"*{lbl}:*")
            for u, _ in imgs:
                desc_lines.append(f"!{u}|align=left!")

    description = "\n".join(desc_lines)
    try:   # an address typed into a free-text field: swap what resolves, never block on it
        from services.user_ref import replace_emails
        description = replace_emails(description)
    except Exception as e:
        logger.error("[TicketCreate] Email replacement in the SO description failed: %s", e)
        description = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email removed]", description)
    issue_key = create_jira_issue(summary=summary, description=description, l1_tags=l1_tags or None)
    if not issue_key:
        return {"ok": False, "error": "Jira did not return an issue key"}
    if comm_link:
        try:
            set_issue_communication_link(issue_key, comm_link)
        except Exception as e:
            logger.error("[TicketCreate] SO comm-link set failed for %s: %s", issue_key, e)
    for bid, f, unique in all_files:
        content = f.get("content")
        if not content:
            continue
        try:
            attach_file_to_jira(issue_key, content, unique, f.get("mimetype", "application/octet-stream"))
        except Exception as e:
            logger.error("[TicketCreate] SO attach %s failed: %s", unique, e)
    logger.info("[TicketCreate] SO %s created (topic: %s)", issue_key, topic)
    return {"ok": True, "key": issue_key, "summary": summary}


# ── URG board (Create Urgent Request) ────────────────────────────────────────────────────────────
# "My Own Ticket" (console Create Urgent): the request is the community manager's own, on their own
# Boosteroid account. The marker line goes into the description / "Additional user" comment so
# services.urg_recipients can tell this recipient apart: at Review it is closed automatically (there
# is no user to reply to) and the creator gets the result in the console + their notify channels.
URG_OWN_MARKER = "Own ticket: community manager's own session (closed automatically, no user to reply to)"
OWN_TESTING_RESULT = "Оновлення гри."      # fixed values requested for the auto-added testing session
OWN_TESTING_TAG = "Game update"


def _save_own_urg(issue_key, own, sessions, author_email, dup):
    """Remember who owns this URG request and log its sessions to testing_sessions exactly like the
    console "Add Testing Session" modal (Slack schema, no Slack post; read by the 08:00 report)."""
    from datetime import datetime
    from services.db import main_db
    sync_db = main_db()
    base = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    ticket_url = f"{base}/browse/{issue_key}" if base else issue_key
    created_by = (author_email.split("@")[0] if author_email else "") or own.get("display") or "console"
    try:
        sync_db["urg_own"].update_one(
            {"_id": f"{issue_key}|{author_email or created_by}"},
            {"$set": {"key": issue_key, "author_email": author_email, "author_display": own.get("display", ""),
                      "user_id": own.get("user_id"), "dup": bool(dup), "created_at": datetime.utcnow()},
             "$setOnInsert": {"notified": False}}, upsert=True)
    except Exception as e:
        logger.error("[TicketCreate] URG own record failed for %s: %s", issue_key, e)
    from services import testing_sessions
    raw = "\n\n".join("ID: %s\nApplication: %s" % (s["session_id"], s["game"]) for s in sessions)
    try:
        ids, err = testing_sessions.add(sync_db["testing_sessions"], raw, "Prod", "Yes", OWN_TESTING_RESULT,
                                        ticket_url, [OWN_TESTING_TAG], created_by)
        if err:
            logger.error("[TicketCreate] URG own testing sessions not saved for %s: %s", issue_key, err)
        n = len(ids)
    except Exception as e:
        n = 0
        logger.error("[TicketCreate] URG own testing session write failed for %s: %s", issue_key, e)
    logger.info("[TicketCreate] URG %s marked own (%s), %d testing session(s) saved", issue_key, created_by, n)
    return n


def _split_session_text(text):
    """(header_text, [block_text, ...]): a block starts at a line beginning with "ID:" (not "User ID:"),
    the same rule as utils.session_parser.parse_session_blocks."""
    header, blocks, cur = [], [], None
    for ln in (text or "").splitlines():
        if ln.strip().lower().startswith("id:"):
            cur = [ln]
            blocks.append(cur)
        elif cur is None:
            header.append(ln)
        else:
            cur.append(ln)
    return "\n".join(header).strip(), ["\n".join(b).strip() for b in blocks]


def _game_key(block_text):
    from utils.session_parser import parse_session_blocks
    _h, ss = parse_session_blocks(block_text)
    s = ss[0] if ss else {}
    return str(s.get("app_id") or "").strip() or (s.get("game") or "").strip().lower()


def create_urgent_ticket(values, author_email, author_display, author_slack_id="", files=None, own=None):
    """One URG ticket per GAME: a URG ticket is about one game, so sessions of different games picked
    together become separate tickets (each with the same header / description / link / screenshots).
    Returns the first ticket's result plus "results" (all of them) when it was split."""
    raw = _v(values, "session_block", "session_input")
    header, blocks = _split_session_text(raw)
    groups, order = {}, []
    for b in blocks:
        k = _game_key(b)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(b)
    if len(order) < 2:
        return _create_urgent_ticket_one(values, author_email, author_display, author_slack_id, files, own)
    import copy
    results = []
    for k in order:
        vals = copy.deepcopy(values)
        part = ((header + "\n\n") if header else "") + "\n\n".join(groups[k])
        vals.setdefault("session_block", {}).setdefault("session_input", {})["value"] = part
        results.append(_create_urgent_ticket_one(vals, author_email, author_display, author_slack_id, files, own))
    oks = [r for r in results if r.get("ok")]
    if not oks:
        return results[0]
    first = dict(oks[0])
    first["results"] = results
    first["keys"] = [r["key"] for r in oks]
    first["summary"] = " | ".join(r.get("summary", "") for r in oks)
    failed = [r.get("error") for r in results if not r.get("ok")]
    if failed:
        first["partial_error"] = "; ".join(e for e in failed if e)
    logger.info("[TicketCreate] URG split into %d tickets by game: %s", len(oks), ", ".join(first["keys"]))
    return first


def _create_urgent_ticket_one(values, author_email, author_display, author_slack_id="", files=None, own=None):
    """Create a URG ticket exactly like handle_create_urgent_request_submit, including the 60-minute
    duplicate check and the Discord/Reddit auto-bind side effects. Returns
    {"ok","key","summary","dup"(bool),"error"}.
    own={"user_id": N, "display": ...} -> "My Own Ticket": the session text is attributed to the CM's
    own User ID, the request is marked own (auto-closed at Review) and logged as a testing session."""
    from utils.session_parser import parse_session_blocks
    from services.jira_service import (
        create_jira_issue, attach_file_to_jira, set_issue_communication_link,
        parse_discord_channel_id, parse_reddit_convo_id, post_jira_comment, find_recent_urg_duplicate,
    )
    from services.mongo_db_service import (
        set_jira_discord_channel_sync, save_jira_ticket_creator_sync, queue_jira_discord_relink_sync,
        save_recent_urg_sync,
    )
    files = files or []
    creator = author_slack_id or author_email or author_display or "console"

    session_raw = _v(values, "session_block", "session_input")
    if own:
        # Attribute the request to the community manager: drop any header (an end user's email / User ID)
        # and put the CM's own User ID on top. The ID comes from their console profile, never the client.
        m0 = re.search(r"(?im)^\s*id:", session_raw)
        _body = session_raw[m0.start():] if m0 else session_raw
        session_raw = f"User ID: {own.get('user_id')}\n\n{_body.strip()}"
    # An email in the session text becomes "User ID N"; an address with no account refuses the ticket.
    session_raw, _uid_err = _ticket_user_id(session_raw, values, _v(values, "comm_link_block", "comm_link_input"))
    if _uid_err:
        return {"ok": False, "error": _uid_err}
    from services.jira_service import JiraURGTicketType, JIRA_URG_TICKET_TYPE_FIELD, urg_ticket_type_enabled
    urg_type = (_s(values, "urg_type_block", "urg_type_input") or "Update").strip()
    if urg_type not in JiraURGTicketType.__members__:
        urg_type = "Update"
    verification = urg_type == "Verification"
    if verification and not urg_ticket_type_enabled():
        return {"ok": False, "error": "Verification tickets are not available yet."}
    update_type = "" if verification else _s(values, "update_type_block", "update_type_input")
    user_description = _v(values, "description_block", "description_input")
    comm_link = _v(values, "comm_link_block", "comm_link_input")

    _header, _sessions = parse_session_blocks(session_raw)
    valid_sessions = [s for s in _sessions if s["game"] and s["playground"]]
    if not valid_sessions:
        return {"ok": False, "error": "Each session needs ID / Application / Playground."}

    app_name = valid_sessions[0]["game"]
    # Keep the game's ID in the URG title, e.g. "Ships At Sea [ID: 1634] - Sao Paulo 2". The ID is the
    # "[ID: n]" the mod pasted on the Application line (parse_session_blocks put it in app_id).
    _first_id = valid_sessions[0].get("app_id")
    _name = f"{app_name} [ID: {_first_id}]" if _first_id else app_name
    playgrounds, app_ids = [], []
    for s in valid_sessions:
        if s["playground"] and s["playground"] not in playgrounds:
            playgrounds.append(s["playground"])
        if s["app_id"] and s["app_id"] not in app_ids:
            app_ids.append(s["app_id"])
    summary = f"{_name} - {'/'.join(playgrounds)}" if playgrounds else _name

    # Session body with the top email linked to the admin panel (like SO)
    email_re = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
    body_lines, email_linked, seen_id = [], False, False
    for ln in session_raw.splitlines():
        if ln.strip().lower().startswith("id:"):
            seen_id = True
        if not seen_id and not email_linked:
            m = email_re.search(ln)
            if m:
                ln = ln[:m.start()] + _admin_email_link(m.group(0)) + ln[m.end():]
                email_linked = True
        if not seen_id and re.fullmatch(r'\s*user id \d{1,12}\s*', ln, re.IGNORECASE):
            ln = "User ID: " + re.search(r'\d+', ln).group(0)      # "User ID 5" (from an email) -> header form
        if not seen_id:
            # A "User ID: 9444330" header line (typed, or added by the console's session picker) links
            # to the same admin page, filtered by that ID.
            um = re.match(r'^(\s*user\s*id\s*:\s*)(\d{1,12})\s*$', ln, re.IGNORECASE)
            if um:
                ln = "User ID: " + um.group(2)          # jira_service.create_jira_issue adds the admin link
                if ln in body_lines:
                    continue                             # an email and its own "User ID:" line -> keep one
        body_lines.append(ln)
    session_body = "\n".join(body_lines)

    # Duplicate within the last hour -> add this user to the existing ticket instead of a new one
    # A Verification ticket never merges into a recent Update ticket for the same game.
    dup_key = None if verification else find_recent_urg_duplicate(app_ids, playgrounds, app_name=app_name, minutes=60)
    if dup_key:
        from services.jira_service import _user_ids_in_text
        comment = f"*Additional user*\n{_user_ids_in_text(session_body)}"
        if own:
            comment += f"\n{URG_OWN_MARKER}"
        if comm_link:
            comment += f"\nCommunication link: {comm_link}"
        post_jira_comment(dup_key, comment)
        save_jira_ticket_creator_sync(dup_key, creator)
        if comm_link:
            dch = parse_discord_channel_id(comm_link)
            if dch:
                queue_jira_discord_relink_sync(dup_key, dch)
            rc = parse_reddit_convo_id(comm_link)
            if rc:
                try:
                    from reddit_notifications.mod_notes import auto_link_jira_from_convo
                    auto_link_jira_from_convo(dup_key, rc)
                except Exception as e:
                    logger.error("[TicketCreate] URG dup reddit auto-link failed for %s: %s", dup_key, e)
        logger.info("[TicketCreate] URG dedup: added user to %s", dup_key)
        if own:
            _save_own_urg(dup_key, own, valid_sessions, author_email, dup=True)
        return {"ok": True, "key": dup_key, "summary": summary, "dup": True, "own": bool(own)}

    author_mention = _author_mention(author_email, author_display)
    desc_parts = [f"*Author:* {author_mention}", session_body, update_type]
    if user_description:
        desc_parts.append(user_description)
    if own:
        desc_parts.append(URG_OWN_MARKER)

    dedup = _dedup_files(files)
    img_refs = [f"!{u}|width=800!" for (f, u) in dedup if (f.get("mimetype", "") or "").startswith("image/")]
    if img_refs:
        desc_parts.append("\n".join(img_refs))
    description = "\n\n".join(p for p in desc_parts if p)

    issue_key = create_jira_issue(
        summary=summary, description=description,
        project_key=os.getenv("JIRA_URG_PROJECT_KEY", "URG"),
        issue_type=os.getenv("JIRA_URG_ISSUE_TYPE", "Task"),
        extra_fields={JIRA_URG_TICKET_TYPE_FIELD: {"id": JiraURGTicketType[urg_type].value}},
    )
    if not issue_key:
        return {"ok": False, "error": "Jira did not return an issue key"}

    if not verification:
        save_recent_urg_sync(issue_key, app_ids, playgrounds, app_name)   # local dedup record (lag-free)
    save_jira_ticket_creator_sync(issue_key, creator)
    if comm_link:
        try:
            set_issue_communication_link(issue_key, comm_link)
        except Exception as e:
            logger.error("[TicketCreate] URG comm-link set failed for %s: %s", issue_key, e)
        dch = parse_discord_channel_id(comm_link)
        if dch:
            set_jira_discord_channel_sync(issue_key, dch)
        rc = parse_reddit_convo_id(comm_link)
        if rc:
            try:
                from reddit_notifications.mod_notes import auto_link_jira_from_convo
                auto_link_jira_from_convo(issue_key, rc)
            except Exception as e:
                logger.error("[TicketCreate] URG reddit auto-link failed for %s: %s", issue_key, e)
    for f, unique in dedup:
        content = f.get("content")
        if not content:
            continue
        try:
            attach_file_to_jira(issue_key, content, unique, f.get("mimetype", "application/octet-stream"))
        except Exception as e:
            logger.error("[TicketCreate] URG attach %s failed: %s", unique, e)
    logger.info("[TicketCreate] URG %s created: %r", issue_key, summary)
    if own:
        _save_own_urg(issue_key, own, valid_sessions, author_email, dup=False)
    return {"ok": True, "key": issue_key, "summary": summary, "dup": False, "own": bool(own)}
