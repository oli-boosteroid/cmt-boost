from views.tags import sorted_issues_tags


def send_community_report_view(private_metadata=""):
    return {
        "type": "modal",
        "callback_id": "send_community_report_submit",
        "title": {"type": "plain_text", "text": "Community Report"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "input",
                "block_id": "send_community_issues_summary_block",
                "optional": False,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "community_report_issues_summary_input",
                    "placeholder": {"type": "plain_text", "text": "Issues summary"}
                },
                "label": {"type": "plain_text", "text": "Issues summary"}
            },
            {
                "type": "input",
                "block_id": "community_tg_posts_sent_block",
                "optional": False,
                "element": {
                    "type": "number_input",
                    "is_decimal_allowed": False,
                    "action_id": "community_tg_posts_sent_input",
                },
                "label": {"type": "plain_text", "text": "Sent posts to TG"}
            },
            {
                "type": "input",
                "block_id": "send_community_testing_sessions_count_block",
                "optional": False,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "community_testing_sessions_count_input",
                    "placeholder": {"type": "plain_text", "text": "Testing sessions count"}
                },
                "label": {"type": "plain_text", "text": "Testing sessions count"}
            },
            {
                "type": "input",
                "block_id": "send_community_testing_sessions_summary_block",
                "optional": False,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "community_testing_sessions_summary_input",
                    "placeholder": {"type": "plain_text", "text": "Testing sessions summary"}
                },
                "label": {"type": "plain_text", "text": "Testing sessions summary"}
            },
            {
                "type": "input",
                "block_id": "send_community_crypto_block",
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "community_crypto_input",
                    "placeholder": {"type": "plain_text", "text": "Crypto Activity"}
                },
                "label": {"type": "plain_text", "text": "Crypto Activity"}
            },
            {
                "type": "input",
                "block_id": "send_community_summary_block",
                "optional": False,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "community_summary_input",
                    "placeholder": {"type": "plain_text", "text": "Day Summary"}
                },
                "label": {"type": "plain_text", "text": "Day Summary"}
            }
        ]
    }


def send_custom_user_issue(private_metadata=""):
    return {
        "type": "modal",
        "callback_id": "send_custom_user_issue_submit",
        "title": {"type": "plain_text", "text": "Send custom issue"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "send_custom_user_issue_post_url_block",
                "optional": False,
                "element": {
                    "type": "url_text_input",
                    "action_id": "send_custom_user_issue_post_url_action",
                    "placeholder": {"type": "plain_text", "text": "Enter issue URL"}
                },
                "label": {"type": "plain_text", "text": "Issue URL"}
            },
            {
                "type": "input",
                "block_id": "send_custom_user_issue_email_block",
                "optional": True,
                "element": {
                    "type": "email_text_input",
                    "action_id": "send_custom_user_issue_email_action",
                    "placeholder": {"type": "plain_text", "text": "user@example.com"}
                },
                "label": {"type": "plain_text", "text": "User ID or email (optional)"}
            },
            {
                "type": "input",
                "block_id": "send_custom_user_issue_facebook_name_block",
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "send_custom_user_issue_facebook_name_action",
                    "placeholder": {"type": "plain_text", "text": "e.g. Vitor Santos"}
                },
                "label": {"type": "plain_text", "text": "Facebook Name (optional, only for Facebook platform)"}
            },
            {
                "type": "input",
                "block_id": "send_custom_user_issue_tags_block",
                "optional": False,
                "element": {
                    "type": "multi_static_select",
                    "action_id": "send_custom_user_issue_tags_action",
                    "placeholder": {"type": "plain_text", "text": "Select tags"},
                    "options": sorted_issues_tags
                },
                "label": {"type": "plain_text", "text": "Tags"}
            },
            {
                "type": "input",
                "block_id": "send_custom_user_issue_ticket_url_block",
                "optional": True,
                "element": {
                    "type": "url_text_input",
                    "action_id": "send_custom_user_issue_ticket_url_action",
                    "placeholder": {"type": "plain_text", "text": "Enter ticket URL"}
                },
                "label": {"type": "plain_text", "text": "Ticket URL"}
            },
            {
                "type": "input",
                "block_id": "send_custom_user_issue_platform_block",
                "optional": False,
                "element": {
                    "type": "static_select",
                    "action_id": "send_custom_user_issue_platform_action",
                    "placeholder": {"type": "plain_text", "text": "Select platform"},
                    "options": [
                        {"text": {"type": "plain_text", "text": "Facebook"}, "value": "Facebook"},
                        {"text": {"type": "plain_text", "text": "Discord"}, "value": "Discord"},
                        {"text": {"type": "plain_text", "text": "Telegram"}, "value": "Telegram"},
                        {"text": {"type": "plain_text", "text": "Reddit"}, "value": "Reddit"},
                    ]
                },
                "label": {"type": "plain_text", "text": "Platform"}
            }
        ]
    }


def send_weekly_report(private_metadata=""):
    return {
        "type": "modal",
        "callback_id": "send_weekly_report_submit",
        "title": {"type": "plain_text", "text": "Weekly Report"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "weekly_technical_problem_block",
                "optional": False,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "weekly_technical_problem_input",
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": "Describe the problems"}
                },
                "label": {"type": "plain_text", "text": "Important/Interesting Technical Problems"}
            },
            {
                "type": "input",
                "block_id": "weekly_non_technical_block",
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "weekly_non_technical_input",
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": "Describe the requests"}
                },
                "label": {"type": "plain_text", "text": "Massive Non-Technical Requests"}
            },
            {
                "type": "input",
                "block_id": "weekly_jira_tickets_block",
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "weekly_jira_tickets_input",
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": "List created Jira tickets"}
                },
                "label": {"type": "plain_text", "text": "Created Jira Tickets"}
            },
            {
                "type": "input",
                "block_id": "weekly_crypto_block",
                "optional": False,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "weekly_crypto_input",
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": "Describe crypto activity"}
                },
                "label": {"type": "plain_text", "text": "Cryptoactivity"}
            },
            {
                "type": "input",
                "block_id": "weekly_other_block",
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "weekly_other_input",
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": "Anything else"}
                },
                "label": {"type": "plain_text", "text": "Other"}
            }
        ]
    }
