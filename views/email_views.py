from views.tags import sorted_issues_tags


def add_discord_modmail_information_modal(private_metadata=""):
    return {
        "type": "modal",
        "callback_id": "add_discord_modmail_information_submit",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Add Information"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "input",
                "block_id": "tag_block",
                "optional": False,
                "element": {
                    "type": "multi_static_select",
                    "action_id": "tag_input",
                    "placeholder": {"type": "plain_text", "text": "Select one or more tags"},
                    "options": sorted_issues_tags,
                },
                "label": {"type": "plain_text", "text": "Tags"}
            },
            {
                "type": "input",
                "block_id": "ticket_url_block",
                "optional": True,
                "element": {
                    "type": "url_text_input",
                    "action_id": "ticket_url_input",
                    "placeholder": {"type": "plain_text", "text": "Enter ticket URL"}
                },
                "label": {"type": "plain_text", "text": "Ticket URL"}
            },
        ]
    }


def add_discord_user_email_modal(discord_user_id: str, discord_username: str):
    return {
        "type": "modal",
        "callback_id": "add_discord_user_email_submit",
        "private_metadata": f"{discord_user_id}:{discord_username}",
        "title": {"type": "plain_text", "text": "Add User ID"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Adding email for Discord user: *{discord_username}*",
                    }
                ],
            },
            {
                "type": "input",
                "block_id": "discord_email_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "discord_email_input",
                    "placeholder": {"type": "plain_text", "text": "User ID, email or Session ID"},
                },
                "label": {"type": "plain_text", "text": "User ID / email / Session ID"},
            },
        ],
    }


def add_reddit_user_email_modal(reddit_username: str):
    return {
        "type": "modal",
        "callback_id": "add_reddit_user_email_submit",
        "private_metadata": reddit_username,
        "title": {"type": "plain_text", "text": "Add User ID"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Adding email for Reddit user: *u/{reddit_username}*",
                    }
                ],
            },
            {
                "type": "input",
                "block_id": "reddit_email_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reddit_email_input",
                    "placeholder": {"type": "plain_text", "text": "User ID or email"},
                },
                "label": {"type": "plain_text", "text": "User ID or email"},
            },
        ],
    }


def add_reddit_jira_ticket_modal(reddit_username: str, convo_id: str = ''):
    return {
        "type": "modal",
        "callback_id": "add_reddit_jira_ticket_submit",
        "private_metadata": f"{reddit_username}:{convo_id}",
        "title": {"type": "plain_text", "text": "Add Jira Ticket"},
        "submit": {"type": "plain_text", "text": "Link"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Linking a Jira ticket to Reddit user: *u/{reddit_username}*",
                    }
                ],
            },
            {
                "type": "input",
                "block_id": "reddit_jira_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reddit_jira_input",
                    "placeholder": {"type": "plain_text", "text": "e.g. SO-12345"},
                },
                "label": {"type": "plain_text", "text": "Jira ticket key"},
            },
        ],
    }


def add_facebook_author_email_modal(author_name: str):
    return {
        "type": "modal",
        "callback_id": "add_facebook_author_email_submit",
        "private_metadata": author_name,
        "title": {"type": "plain_text", "text": "Add User ID"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Adding email for Facebook author: *{author_name}*",
                    }
                ],
            },
            {
                "type": "input",
                "block_id": "facebook_email_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "facebook_email_input",
                    "placeholder": {"type": "plain_text", "text": "User ID or email"},
                },
                "label": {"type": "plain_text", "text": "User ID or email"},
            },
        ],
    }


def add_email_manual_modal(channel_id: str):
    return {
        "type": "modal",
        "callback_id": "add_email_manual_submit",
        "private_metadata": channel_id,
        "title": {"type": "plain_text", "text": "Add User ID"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "add_email_manual_email_block",
                "element": {
                    "type": "email_text_input",
                    "action_id": "add_email_manual_email_action",
                    "placeholder": {"type": "plain_text", "text": "user@example.com"},
                },
                "label": {"type": "plain_text", "text": "User ID or email"},
            },
            {
                "type": "input",
                "block_id": "add_email_manual_url_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "add_email_manual_url_action",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "discord.com/users/... | reddit.com/user/... | facebook.com/...",
                    },
                },
                "label": {"type": "plain_text", "text": "Profile URL"},
            },
        ],
    }


def search_email_by_profile_url_modal(channel_id: str):
    return {
        "type": "modal",
        "callback_id": "search_email_by_profile_url_submit",
        "private_metadata": channel_id,
        "title": {"type": "plain_text", "text": "Search User ID"},
        "submit": {"type": "plain_text", "text": "Search"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "search_email_url_block",
                "optional": True,
                "element": {
                    "type": "url_text_input",
                    "action_id": "search_email_url_action",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "https://www.reddit.com/user/... or discord.com/users/..."
                    }
                },
                "label": {"type": "plain_text", "text": "Profile URL (Reddit / Discord)"}
            },
            {
                "type": "input",
                "block_id": "search_email_facebook_name_block",
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "search_email_facebook_name_action",
                    "placeholder": {"type": "plain_text", "text": "e.g. Vitor Santos"}
                },
                "label": {"type": "plain_text", "text": "Facebook Name"}
            }
        ]
    }
