"""Maintenance-notification modals (game + server).

A "Type" selector (Discord / Reddit) drives the extra field: a single link input (Discord ticket
URL or Reddit ModMail URL). The link field stays hidden until a Type is chosen (the block-action handler
re-renders the modal via views_update). All labels/text are in English.
"""

_LANG_OPTIONS = [
    {"text": {"type": "plain_text", "text": "English"}, "value": "en"},
    {"text": {"type": "plain_text", "text": "Ukrainian"}, "value": "ua"},
    {"text": {"type": "plain_text", "text": "Russian"}, "value": "ru"},
]

_TYPE_OPTIONS = [
    {"text": {"type": "plain_text", "text": "Discord"}, "value": "discord"},
    {"text": {"type": "plain_text", "text": "Reddit"}, "value": "reddit"},
]


def _option(text, value):
    return {"text": {"type": "plain_text", "text": text}, "value": value}


def _find_option(options, value):
    return next((o for o in options if o["value"] == value), None)


def _type_block(delivery_type=None):
    el = {
        "type": "static_select",
        "action_id": "notification_delivery_type",
        "placeholder": {"type": "plain_text", "text": "Select type"},
        "options": _TYPE_OPTIONS,
    }
    init = _find_option(_TYPE_OPTIONS, delivery_type) if delivery_type else None
    if init:
        el["initial_option"] = init
    return {"type": "actions", "block_id": "delivery_type_block", "elements": [el]}


def _language_block(selected=None):
    el = {
        "type": "static_select",
        "action_id": "notification_language_input",
        "placeholder": {"type": "plain_text", "text": "Language"},
        "options": _LANG_OPTIONS,
    }
    init = _find_option(_LANG_OPTIONS, selected) if selected else None
    if init:
        el["initial_option"] = init
    return {
        "type": "input",
        "block_id": "send_notification_language_block",
        "optional": False,
        "element": el,
        "label": {"type": "plain_text", "text": "Language"},
    }


def _entity_block(kind, options, selected=None):
    action_id = "notification_game_input" if kind == "game" else "notification_server_input"
    block_id = "send_notification_game_block" if kind == "game" else "send_notification_server_block"
    label = "Game in maintenance" if kind == "game" else "Server in maintenance"
    if options:
        opts = [_option(o, o) for o in options]
        el = {
            "type": "static_select",
            "action_id": action_id,
            "placeholder": {"type": "plain_text", "text": f"Select {kind}"},
            "options": opts,
        }
        init = _find_option(opts, selected) if selected else None
        if init:
            el["initial_option"] = init
    else:
        el = {
            "type": "static_select",
            "action_id": action_id,
            "placeholder": {"type": "plain_text", "text": f"No {kind}s in maintenance"},
            "options": [_option(f"No {kind}s in maintenance", "__none__")],
        }
    return {
        "type": "input",
        "block_id": block_id,
        "optional": False,
        "element": el,
        "label": {"type": "plain_text", "text": label},
    }


def _link_block():
    return {
        "type": "input",
        "block_id": "send_notification_link_block",
        "optional": False,
        "element": {
            "type": "plain_text_input",
            "action_id": "notification_link_input",
            "placeholder": {"type": "plain_text", "text": "Discord ticket URL or Reddit ModMail URL"},
        },
        "label": {"type": "plain_text", "text": "Discord/Reddit link"},
    }


def build_maintenance_modal(kind, entity_options=None, private_metadata="",
                            delivery_type=None, selected_entity=None, selected_language=None):
    """Builds the game or server maintenance modal. `delivery_type` decides the extra field."""
    entity_options = entity_options or []
    callback_id = ("send_maintenance_notification_submit" if kind == "game"
                   else "send_server_maintenance_notification_submit")
    title = "Game Maintenance" if kind == "game" else "Server Maintenance"

    blocks = [
        _type_block(delivery_type),
        _entity_block(kind, entity_options, selected_entity),
        _language_block(selected_language),
    ]
    if delivery_type in ("discord", "reddit"):
        blocks.append(_link_block())

    return {
        "type": "modal",
        "callback_id": callback_id,
        "private_metadata": private_metadata or "",
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": blocks,
    }


def send_maintenance_notification(games_list: list = None, private_metadata=""):
    return build_maintenance_modal("game", entity_options=games_list, private_metadata=private_metadata)


def send_server_maintenance_notification(servers_list: list = None, private_metadata=""):
    return build_maintenance_modal("server", entity_options=servers_list, private_metadata=private_metadata)
