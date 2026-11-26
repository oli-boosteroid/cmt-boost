def send_game_proposal_view(private_metadata=""):
    return {
        "type": "modal",
        "callback_id": "send_game_proposal_submit",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Send user's proposal"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "input",
                "block_id": "send_game_block",
                "optional": False,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "game_input",
                    "placeholder": {"type": "plain_text", "text": "The Game"}
                },
                "label": {"type": "plain_text", "text": "The Game"}
            },
            {
                "type": "input",
                "block_id": "send_game_platform_block",
                "optional": False,
                "element": {
                    "type": "static_select",
                    "action_id": "send_game_platform_input",
                    "placeholder": {"type": "plain_text", "text": "Platform"},
                    "options": [
                        {"text": {"type": "plain_text", "text": "Steam"}, "value": "Steam"},
                        {"text": {"type": "plain_text", "text": "Epic"}, "value": "Epic"},
                        {"text": {"type": "plain_text", "text": "Xbox"}, "value": "Xbox"},
                        {"text": {"type": "plain_text", "text": "EA"}, "value": "EA"},
                        {"text": {"type": "plain_text", "text": "Battle.net"}, "value": "Battle.net"},
                        {"text": {"type": "plain_text", "text": "Ubisoft"}, "value": "Ubisoft"},
                        {"text": {"type": "plain_text", "text": "Rockstar"}, "value": "Rockstar"},
                        {"text": {"type": "plain_text", "text": "Other"}, "value": "Other"},
                    ]
                },
                "label": {"type": "plain_text", "text": "Platform"}
            },
        ]
    }


def send_proposal_view(private_metadata=""):
    return {
        "type": "modal",
        "callback_id": "send_proposal_submit",
        "title": {"type": "plain_text", "text": "Send user's proposal"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "input",
                "block_id": "send_proposal_text_block",
                "optional": False,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "proposal_input",
                    "placeholder": {"type": "plain_text", "text": "Describe the Proposal"}
                },
                "label": {"type": "plain_text", "text": "Describe the Proposal"}
            },
            {
                "type": "input",
                "block_id": "send_proposal_type_block",
                "optional": False,
                "element": {
                    "type": "static_select",
                    "action_id": "send_proposal_type_input",
                    "placeholder": {"type": "plain_text", "text": "Type of Proposal"},
                    "options": [
                        {"text": {"type": "plain_text", "text": "Feature"}, "value": "Feature"},
                        {"text": {"type": "plain_text", "text": "Payment Method"}, "value": "Payment Method"},
                        {"text": {"type": "plain_text", "text": "Feedback"}, "value": "Feedback"},
                        {"text": {"type": "plain_text", "text": "Gamepad"}, "value": "Gamepad"},
                        {"text": {"type": "plain_text", "text": "Proposal"}, "value": "Proposal"},
                    ]
                },
                "label": {"type": "plain_text", "text": "Type of Proposal"}
            },
        ]
    }
