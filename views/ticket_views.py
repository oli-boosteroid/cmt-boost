from views.helpers import _opt

_JIRA_TOPICS = [
    "Poor quality/lags",
    "Game issue",
    "Launcher issue",
    "Desktop application issue",
    "TV application issue",
    "Saves issue",
    "DLC request",
    "Local saves/config support request",
    "Gamepad issue",
    "Subscription issue",
    "Account issue",
    "Site issue",
    "Other",
]

# Slack modal title limit is 24 chars - map long topic names to short display titles
_TOPIC_TITLES: dict[str, str] = {
    "Desktop application issue": "Desktop App Issue",
    "Local saves/config support request": "Local Request",
}


def _ti(block_id: str, action_id: str, label: str, placeholder: str = "", optional: bool = False) -> dict:
    """Single-line plain_text_input block."""
    el = {"type": "plain_text_input", "action_id": action_id}
    if placeholder:
        el["placeholder"] = {"type": "plain_text", "text": placeholder}
    return {
        "type": "input",
        "block_id": block_id,
        "optional": optional,
        "element": el,
        "label": {"type": "plain_text", "text": label},
    }


def _ti_ml(block_id: str, action_id: str, label: str, placeholder: str = "", optional: bool = False) -> dict:
    """Multiline plain_text_input block."""
    el = {"type": "plain_text_input", "action_id": action_id, "multiline": True}
    if placeholder:
        el["placeholder"] = {"type": "plain_text", "text": placeholder}
    return {
        "type": "input",
        "block_id": block_id,
        "optional": optional,
        "element": el,
        "label": {"type": "plain_text", "text": label},
    }


def _sel(block_id: str, action_id: str, label: str, options: list, optional: bool = False) -> dict:
    """static_select input block."""
    return {
        "type": "input",
        "block_id": block_id,
        "optional": optional,
        "element": {
            "type": "static_select",
            "action_id": action_id,
            "placeholder": {"type": "plain_text", "text": f"Select {label.lower()}"},
            "options": [_opt(o) for o in options],
        },
        "label": {"type": "plain_text", "text": label},
    }


def _fi(block_id: str, action_id: str, label: str, max_files: int = 5, any_file: bool = False) -> dict:
    """file_input block (optional). By default accepts images/video; any_file=True accepts all types."""
    el: dict = {"type": "file_input", "action_id": action_id, "max_files": max_files}
    if not any_file:
        el["filetypes"] = ["jpg", "jpeg", "png", "gif", "mp4", "mov", "webm"]
    return {
        "type": "input",
        "block_id": block_id,
        "optional": True,
        "element": el,
        "label": {"type": "plain_text", "text": label},
    }


# Placeholder + hint shared by every "Session data" input, documenting the
# extended client format (Playground/Device/Client optional, multiple sessions).
_SESSION_PLACEHOLDER = ("ID: 4a6a8afd-...\nApplication: Game name [ID: 682]\nPlayground: Krakow"
                        "\n\nDevice: Desktop\nClient: BoosteroidWindowsClient v.1.11.23")
_SESSION_HINT = ('Prefixes: "ID:", "Application:", "Playground:" ("Device:"/"Client:" optional). '
                 'Paste multiple sessions back to back.')


def _session_data_block(optional: bool = False) -> dict:
    """The 'Session data' input block, accepting one or more sessions in the extended format."""
    return {
        "type": "input",
        "block_id": "app_id_playground_block",
        "optional": optional,
        "element": {
            "type": "plain_text_input",
            "action_id": "app_id_playground_input",
            "multiline": True,
            "placeholder": {"type": "plain_text", "text": _SESSION_PLACEHOLDER},
        },
        "label": {"type": "plain_text", "text": "Session data"},
        "hint": {"type": "plain_text", "text": _SESSION_HINT},
    }


def _common_fields(optional_session: bool = False, ticket_name_label: str = "Ticket Name") -> list:
    """Ticket Name + Email + Communication Link + Session data - shared by most topics."""
    return [
        _ti("ticket_name_block", "ticket_name_input", ticket_name_label),
        _ti("email_block", "email_input", "User ID or email", placeholder="7436618"),
        _ti("comm_link_block", "comm_link_input", "Communication Link",
            placeholder="https://discord.com/channels/...", optional=True),
        _session_data_block(optional=optional_session),
    ]


def _topic_blocks(topic: str) -> list:
    """Returns the list of input blocks for the given topic (without Author section)."""
    yn_dash = ["Yes", "No", "-"]
    yn = ["Yes", "No"]

    if topic == "Poor quality/lags":
        return [
            _ti("ticket_name_block", "ticket_name_input", "Ticket Name"),
            _ti("email_block", "email_input", "User ID or email", placeholder="7436618"),
            _ti("comm_link_block", "comm_link_input", "Communication Link",
                placeholder="https://discord.com/channels/...", optional=True),
            _session_data_block(),
            _sel("connection_block", "connection_input", "Connection type",
                 ["Ethernet", "Wi-Fi 5 GHz", "Wi-Fi 2.4 GHz"]),
            _sel("multiple_games_block", "multiple_games_input", "Issue is present in multiple games", yn_dash),
            _sel("other_devices_block", "other_devices_input", "Issue appears on other devices/networks", yn),
            _sel("issue_origin_block", "issue_origin_input", "Where is the issue appears",
                 ["Browser", "Application", "Both application and browser", "-"]),
            _sel("third_party_block", "third_party_input",
                 "Is there an additional load on the network while streaming", yn_dash),
            _fi("speedtest_block", "speedtest_input", "Speedtest", max_files=3),
            _fi("mtr_block", "mtr_input", "MTR", max_files=3),
            _fi("mtu_block", "mtu_input", "MTU", max_files=3),
            _fi("screenshots_block", "screenshots_input", "Screenshot/video of the problem"),
            _ti_ml("steps_block", "steps_input", "Steps already taken", optional=True),
            _ti_ml("description_block", "description_input", "Description"),
        ]

    if topic == "Game issue":
        return [
            _ti("ticket_name_block", "ticket_name_input", "Problem summary (will be added to ticket name)"),
            _ti("email_block", "email_input", "User ID or email", placeholder="7436618"),
            _ti("comm_link_block", "comm_link_input", "Communication Link",
                placeholder="https://discord.com/channels/...", optional=True),
            _session_data_block(),
            _sel("issue_origin_block", "issue_origin_input", "Where is the issue appears",
                 ["Browser", "Application", "Both application and browser", "-"]),
            _fi("media_block", "media_input", "Screenshot/video of the problem"),
            _ti_ml("steps_block", "steps_input", "Steps already taken", optional=True),
            _ti_ml("description_block", "description_input", "Description"),
        ]

    if topic == "Launcher issue":
        return _common_fields(ticket_name_label="Problem summary (will be added to ticket name)") + [
            _sel("launcher_block", "launcher_input", "Launcher",
                 ["Steam", "Epic", "Xbox", "EA", "Battle.net", "Ubisoft", "Rockstar", "Other"]),
            _fi("media_block", "media_input", "Screenshot/video of the problem"),
            _ti_ml("steps_block", "steps_input", "Steps already taken", optional=True),
            _ti_ml("description_block", "description_input", "Description"),
        ]

    if topic == "Desktop application issue":
        return _common_fields(optional_session=True, ticket_name_label="Problem summary (will be added to ticket name)") + [
            _sel("application_block", "application_input", "Application",
                 ["Windows app", "macOS app", "Linux app"]),
            _fi("media_block", "media_input", "Screenshot/video of the problem"),
            _fi("log_block", "log_input", "Logs", max_files=3, any_file=True),
            _ti_ml("steps_block", "steps_input", "Steps already taken", optional=True),
            _ti_ml("description_block", "description_input", "Description"),
        ]

    if topic == "TV application issue":
        return _common_fields(optional_session=True, ticket_name_label="Problem summary (will be added to ticket name)") + [
            _sel("application_block", "application_input", "Application",
                 ["Android TV", "Samsung TV", "LG TV", "Philips TV", "Hisense TV", "Fire TV"]),
            _fi("media_block", "media_input", "Screenshot/video of the problem"),
            _fi("tv_os_block", "tv_os_input", "TV OS version screenshot"),
            _ti_ml("steps_block", "steps_input", "Steps already taken", optional=True),
            _ti_ml("description_block", "description_input", "Description"),
        ]

    if topic == "Saves issue":
        return _common_fields(ticket_name_label="Problem summary (will be added to ticket name)") + [
            _sel("saves_block", "saves_input", "Saves type", ["Local", "Cloud"]),
            _sel("multiple_games_block", "multiple_games_input", "Issue is present in multiple games", yn_dash),
            _sel("account_type_block", "account_type_input", "What type of an account user has",
                 ["Own account", "Shared account"]),
            _fi("steam_remote_block", "steam_remote_input", "Screenshot of Steam Remote Storage (if Steam game)", max_files=3),
            _fi("screenshots_block", "screenshots_input", "Screenshot/video of the problem"),
            _ti_ml("steps_block", "steps_input", "Steps already taken", optional=True),
            _ti_ml("description_block", "description_input", "Description"),
        ]

    if topic == "DLC request":
        return _common_fields(optional_session=True, ticket_name_label="Game Name (will be added to ticket name)") + [
            _ti("dlc_name_block", "dlc_name_input", "DLC name/link"),
            _fi("dlc_block", "dlc_input", "DLCs screenshot"),
            _ti_ml("description_block", "description_input", "Description"),
        ]

    if topic == "Local saves/config support request":
        return [
            _ti("email_block", "email_input", "User ID or email", placeholder="7436618"),
            _ti("comm_link_block", "comm_link_input", "Communication Link",
                placeholder="https://discord.com/channels/...", optional=True),
            _session_data_block(),
            _sel("local_request_type_block", "local_request_type_input", "Local request type",
                 ["Config", "Local saves", "Local saves & config"]),
            _ti_ml("description_block", "description_input", "Description"),
        ]

    if topic == "Gamepad issue":
        return _common_fields() + [
            _ti("gamepad_model_block", "gamepad_model_input", "Gamepad model"),
            _sel("gamepad_connection_block", "gamepad_connection_input", "Connection type",
                 ["Cable", "Bluetooth"]),
            _sel("multiple_games_block", "multiple_games_input", "Issue is present in multiple games", yn_dash),
            _sel("issue_origin_block", "issue_origin_input", "Where is the issue appears",
                 ["Browser", "Application", "Both application and browser", "-"]),
            _fi("media_block", "media_input", "Screenshot/video of the issue"),
            _fi("gamepad_tester_block", "gamepad_tester_input", "Screenshot/video of the Gamepad Tester site"),
            _ti_ml("steps_block", "steps_input", "Steps already taken", optional=True),
            _ti_ml("description_block", "description_input", "Description"),
        ]

    if topic == "Subscription issue":
        return [
            _ti("ticket_name_block", "ticket_name_input", "Ticket Name"),
            _ti("email_block", "email_input", "User ID or email", placeholder="7436618"),
            _ti("comm_link_block", "comm_link_input", "Communication Link",
                placeholder="https://discord.com/channels/...", optional=True),
            _ti("uniq_id_block", "uniq_id_input", "Uniq ID", placeholder="User unique ID"),
            _ti_ml("steps_block", "steps_input", "Steps already taken", optional=True),
            _ti_ml("description_block", "description_input", "Description"),
        ]

    if topic == "Account issue":
        return [
            _ti("ticket_name_block", "ticket_name_input", "Ticket Name"),
            _ti("email_block", "email_input", "User ID or email", placeholder="7436618"),
            _ti("comm_link_block", "comm_link_input", "Communication Link",
                placeholder="https://discord.com/channels/...", optional=True),
            _ti_ml("steps_block", "steps_input", "Steps already taken", optional=True),
            _ti_ml("description_block", "description_input", "Description"),
        ]

    if topic == "Site issue":
        return [
            _ti("ticket_name_block", "ticket_name_input", "Ticket Name"),
            _ti("comm_link_block", "comm_link_input", "Communication Link",
                placeholder="https://discord.com/channels/...", optional=True),
            _fi("media_block", "media_input", "Screenshot/video of the problem"),
            _fi("console_block", "console_input", "Screenshot of browser console"),
            _ti_ml("steps_block", "steps_input", "Steps already taken", optional=True),
            _ti_ml("description_block", "description_input", "Description"),
        ]

    if topic == "Other":
        return _common_fields(optional_session=True) + [
            _fi("media_block", "media_input", "Files"),
            _ti_ml("steps_block", "steps_input", "Steps already taken", optional=True),
            _ti_ml("description_block", "description_input", "Description"),
        ]

    # Generic fallback
    return _common_fields() + [
        _ti_ml("steps_block", "steps_input", "Steps already taken", optional=True),
        _ti_ml("description_block", "description_input", "Description"),
    ]


def search_discord_tickets_modal(channel_id: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "search_discord_tickets_submit",
        "private_metadata": channel_id,
        "title": {"type": "plain_text", "text": "Search Discord Tickets"},
        "submit": {"type": "plain_text", "text": "Search"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "discord_search_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "discord_search_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "username, username#1234, or numeric user ID",
                    },
                },
                "label": {"type": "plain_text", "text": "Discord username or user ID"},
                "hint": {
                    "type": "plain_text",
                    "text": "Partial username match is supported (case-insensitive). "
                            "Enter a numeric ID for exact user lookup.",
                },
            }
        ],
    }


def create_ticket_topic_modal() -> dict:
    """Step 1 - topic selection modal."""
    return {
        "type": "modal",
        "callback_id": "create_ticket_topic_select",
        "title": {"type": "plain_text", "text": "Create Jira Ticket"},
        "submit": {"type": "plain_text", "text": "Next ->"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "topic_block",
                "element": {
                    "type": "static_select",
                    "action_id": "topic_input",
                    "placeholder": {"type": "plain_text", "text": "Select topic"},
                    "options": [
                        {"text": {"type": "plain_text", "text": t}, "value": t}
                        for t in _JIRA_TOPICS
                    ],
                },
                "label": {"type": "plain_text", "text": "Ticket topic"},
            }
        ],
    }


def create_ticket_fields_modal(topic: str, author_display: str) -> dict:
    """Step 2 - topic-specific fields (Communication Link is inside each topic block)."""
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Author:* @{author_display}"},
        },
        {"type": "divider"},
    ]
    blocks += _topic_blocks(topic)
    blocks.append(
        {
            "type": "input",
            "block_id": "l1_tags_block",
            "optional": False,
            "element": {
                "type": "multi_static_select",
                "action_id": "l1_tags_input",
                "placeholder": {"type": "plain_text", "text": "Select one or more tags"},
                "options": [
                    {"text": {"type": "plain_text", "text": tag}, "value": tag}
                    for tag in [
                        "native_desktop", "native_android", "webclient", "web_tv",
                        "game", "launcher", "subscription", "stream",
                        "web_backend", "tbd",
                    ]
                ],
            },
            "label": {"type": "plain_text", "text": "L1 Tags"},
        }
    )
    return {
        "type": "modal",
        "callback_id": "create_ticket_submit",
        "private_metadata": topic,
        "title": {"type": "plain_text", "text": _TOPIC_TITLES.get(topic, topic[:24])},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


_URG_SESSION_PLACEHOLDER = (
    "User ID: 7436618\n\n"
    "ID: 4a6a8afd-...\nApplication: Cyberpunk 2077 (Steam) [ID: 682]\nPlayground: Ploiesti"
)


def create_urgent_request_modal(author_display: str) -> dict:
    """Create Urgent Request modal - creates a ticket on the URG Jira board."""
    return {
        "type": "modal",
        "callback_id": "create_urgent_request_submit",
        "title": {"type": "plain_text", "text": "Urgent Request"},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Author:* @{author_display}"},
            },
            {"type": "divider"},
            {
                "type": "input",
                "block_id": "session_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "session_input",
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": _URG_SESSION_PLACEHOLDER},
                },
                "label": {"type": "plain_text", "text": "Session info"},
                "hint": {"type": "plain_text", "text": "Optional User ID on top. "
                         "Paste one session, or several for multiple playgrounds."},
            },
            _sel("update_type_block", "update_type_input", "Update type",
                 ["Update in launcher", "Update in game", "Version mismatch"]),
            _ti_ml("description_block", "description_input", "Description", optional=True),
            _fi("screenshot_block", "screenshot_input", "Screenshot", max_files=10),
            _ti("comm_link_block", "comm_link_input", "Communication Link",
                placeholder="https://discord.com/channels/...", optional=True),
        ],
    }
