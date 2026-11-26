def steam_catalog_modal(private_metadata=""):
    return {
        "type": "modal",
        "callback_id": "steam_catalog_submit",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Collect Game Data"},
        "submit": {"type": "plain_text", "text": "Collect"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Paste one or more Steam game URLs (one per line). The bot will collect all game data and 6 screenshots.",
                },
            },
            {
                "type": "input",
                "block_id": "steam_urls_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "steam_urls_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": (
                            "https://store.steampowered.com/app/1234567/Game_Name/\n"
                            "https://store.steampowered.com/app/7654321/Another_Game/"
                        ),
                    },
                },
                "label": {"type": "plain_text", "text": "Steam URLs"},
            },
            {
                "type": "input",
                "block_id": "platform_block",
                "element": {
                    "type": "static_select",
                    "action_id": "platform_input",
                    "placeholder": {"type": "plain_text", "text": "Select platform"},
                    "options": [
                        {"text": {"type": "plain_text", "text": "Steam"}, "value": "Steam"},
                        {"text": {"type": "plain_text", "text": "Epic Games"}, "value": "Epic Games"},
                        {"text": {"type": "plain_text", "text": "Xbox"}, "value": "Xbox"},
                        {"text": {"type": "plain_text", "text": "Ubisoft"}, "value": "Ubisoft"},
                    ],
                },
                "label": {"type": "plain_text", "text": "Platform"},
            },
            {
                "type": "input",
                "block_id": "section_type_block",
                "element": {
                    "type": "static_select",
                    "action_id": "section_type_input",
                    "placeholder": {"type": "plain_text", "text": "Select section"},
                    "options": [
                        {"text": {"type": "plain_text", "text": "Library"}, "value": "Library"},
                        {"text": {"type": "plain_text", "text": "Install"}, "value": "Install"},
                    ],
                },
                "label": {"type": "plain_text", "text": "Section"},
            },
        ],
    }
