from views.tags import sorted_testing_tags
from views.helpers import _opt


def add_testing_session(private_metadata="", show_issue_fields=False, state=None):
    """
    Builds the Add Testing Session modal.

    show_issue_fields - when True, adds Result (optional) and Issue Tags blocks.
    state             - body["view"]["state"]["values"] from a block_actions event,
                        used to preserve already-entered values when updating the modal.
    """
    s = state or {}

    def _text(block_id, action_id):
        return s.get(block_id, {}).get(action_id, {}).get("value") or ""

    def _sel(block_id, action_id):
        """Return initial_option dict preserving current selection, or None."""
        opt = s.get(block_id, {}).get(action_id, {}).get("selected_option")
        if opt:
            return _opt(opt["value"])
        return None

    def _multisel_opts(block_id, action_id):
        """Return matching options from sorted_testing_tags for current selection."""
        selected = {o["value"] for o in s.get(block_id, {}).get(action_id, {}).get("selected_options", [])}
        return [o for o in sorted_testing_tags if o["value"] in selected]

    session_val     = _text("session_block",    "session_input")
    ticket_val      = _text("ticket_url_block", "ticket_url_input")
    environment_sel = _sel("environment_block", "environment_input")
    issue_sel       = _sel("issue_block",       "issue_input")

    blocks = [
        {
            "type": "input",
            "block_id": "session_block",
            "optional": False,
            "element": {
                "type": "plain_text_input",
                "action_id": "session_input",
                "multiline": True,
                "placeholder": {"type": "plain_text", "text": "ID: 03d7005e-a318-4eb6-9230-3a060fc10bb1\nApplication: Crimson Desert\n\nID: 1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d\nApplication: Other Game"},
                **({"initial_value": session_val} if session_val else {}),
            },
            "label": {"type": "plain_text", "text": "Session Info"},
            "hint":  {"type": "plain_text", "text": 'Each session starts with "ID:" then "Application:". Extra lines (Playground/Device/Client) are accepted. Paste multiple sessions back to back.'},
        },
        {
            "type": "input",
            "block_id": "ticket_url_block",
            "optional": True,
            "element": {
                "type": "url_text_input",
                "action_id": "ticket_url_input",
                "placeholder": {"type": "plain_text", "text": "Enter ticket URL"},
                **({"initial_value": ticket_val} if ticket_val else {}),
            },
            "label": {"type": "plain_text", "text": "Ticket URL"},
        },
        {
            "type": "input",
            "block_id": "environment_block",
            "optional": False,
            "element": {
                "type": "static_select",
                "action_id": "environment_input",
                "placeholder": {"type": "plain_text", "text": "Environment"},
                "options": [_opt("Prod"), _opt("Testing")],
                **({"initial_option": environment_sel} if environment_sel else {}),
            },
            "label": {"type": "plain_text", "text": "Environment"},
        },
        {
            "type": "input",
            "block_id": "issue_block",
            "dispatch_action": True,
            "optional": False,
            "element": {
                "type": "static_select",
                "action_id": "issue_input",
                "placeholder": {"type": "plain_text", "text": "Issue"},
                "options": [_opt("Yes"), _opt("No")],
                **({"initial_option": issue_sel} if issue_sel else {}),
            },
            "label": {"type": "plain_text", "text": "Issue"},
        },
    ]

    if show_issue_fields:
        result_val   = _text("result_block", "result_input")
        initial_tags = _multisel_opts("testing_tags_block", "testing_tags_input")

        blocks.append({
            "type": "input",
            "block_id": "result_block",
            "optional": True,
            "element": {
                "type": "plain_text_input",
                "action_id": "result_input",
                "placeholder": {"type": "plain_text", "text": "Result"},
                **({"initial_value": result_val} if result_val else {}),
            },
            "label": {"type": "plain_text", "text": "Result"},
        })
        blocks.append({
            "type": "input",
            "block_id": "testing_tags_block",
            "optional": True,
            "element": {
                "type": "multi_static_select",
                "action_id": "testing_tags_input",
                "placeholder": {"type": "plain_text", "text": "Issue Tags"},
                "options": sorted_testing_tags,
                **({"initial_options": initial_tags} if initial_tags else {}),
            },
            "label": {"type": "plain_text", "text": "Issue tags"},
        })

    return {
        "type": "modal",
        "callback_id": "add_testing_session_submit",
        "private_metadata": private_metadata,
        "title":  {"type": "plain_text", "text": "Add Testing Session"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close":  {"type": "plain_text", "text": "Close"},
        "blocks": blocks,
    }


def edit_testing_session_modal(session: dict, mongo_id: str):
    def option(value):
        return {"text": {"type": "plain_text", "text": str(value)}, "value": str(value)}

    initial_tags = [opt for opt in sorted_testing_tags if opt["value"] in session.get("testing_tags", [])]

    return {
        "type": "modal",
        "callback_id": "edit_testing_session_submit",
        "private_metadata": mongo_id,
        "title": {"type": "plain_text", "text": "Edit Testing Session"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "session_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "session_input",
                    "multiline": True,
                    "initial_value": f"ID: {session.get('session_id', '')}\nApplication: {session.get('game', '')}",
                },
                "label": {"type": "plain_text", "text": "Session Info"},
                "hint": {"type": "plain_text", "text": "Each value on its own line. Prefixes must be exact: \"ID:\" and \"Application:\". Extra lines (Playground/Device/Client) are accepted."}
            },
            {
                "type": "input",
                "block_id": "ticket_url_block",
                "optional": True,
                "element": {
                    "type": "url_text_input",
                    "action_id": "ticket_url_input",
                    **({"initial_value": session["ticket_url"]} if session.get("ticket_url") else {}),
                },
                "label": {"type": "plain_text", "text": "Ticket URL"}
            },
            {
                "type": "input",
                "block_id": "environment_block",
                "element": {
                    "type": "static_select",
                    "action_id": "environment_input",
                    "initial_option": option(session.get("environment", "Prod")),
                    "options": [option("Prod"), option("Testing")],
                },
                "label": {"type": "plain_text", "text": "Environment"}
            },
            {
                "type": "input",
                "block_id": "issue_block",
                "element": {
                    "type": "static_select",
                    "action_id": "issue_input",
                    "initial_option": option(session.get("issue", "No")),
                    "options": [option("Yes"), option("No")],
                },
                "label": {"type": "plain_text", "text": "Issue"}
            },
            {
                "type": "input",
                "block_id": "result_block",
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "result_input",
                    **({"initial_value": session["result"]} if session.get("result") else {}),
                },
                "label": {"type": "plain_text", "text": "Result"}
            },
            {
                "type": "input",
                "block_id": "testing_tags_block",
                "optional": True,
                "element": {
                    "type": "multi_static_select",
                    "action_id": "testing_tags_input",
                    "options": sorted_testing_tags,
                    **({"initial_options": initial_tags} if initial_tags else {}),
                },
                "label": {"type": "plain_text", "text": "Issue tags"}
            },
        ]
    }
