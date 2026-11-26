from views.tags import sorted_issues_tags

# Re-export everything from sub-modules so that existing imports keep working.
from views.testing_views import add_testing_session, edit_testing_session_modal
from views.maintenance_views import send_maintenance_notification, send_server_maintenance_notification
from views.proposal_views import send_game_proposal_view, send_proposal_view
from views.report_views import (
    send_community_report_view,
    send_custom_user_issue,
    send_weekly_report,
)
from views.email_views import (
    add_discord_modmail_information_modal,
    add_discord_user_email_modal,
    add_reddit_user_email_modal,
    add_facebook_author_email_modal,
    search_email_by_profile_url_modal,
    add_email_manual_modal,
)
from views.ticket_views import (
    search_discord_tickets_modal,
    create_ticket_topic_modal,
    create_ticket_fields_modal,
    create_urgent_request_modal,
)

__all__ = [
    "sorted_issues_tags", "add_information_modal",
    "add_testing_session", "edit_testing_session_modal",
    "send_maintenance_notification", "send_server_maintenance_notification",
    "send_game_proposal_view", "send_proposal_view",
    "send_community_report_view", "send_custom_user_issue", "send_weekly_report",
    "add_discord_modmail_information_modal", "add_discord_user_email_modal", "add_reddit_user_email_modal",
    "add_facebook_author_email_modal", "search_email_by_profile_url_modal", "add_email_manual_modal",
    "search_discord_tickets_modal", "create_ticket_topic_modal", "create_ticket_fields_modal",
    "create_urgent_request_modal",
]


def add_information_modal(private_metadata=""):
    return {
        "type": "modal",
        "callback_id": "add_information_modal_submit",
        "private_metadata": private_metadata,
        "title": {
            "type": "plain_text",
            "text": "Add Post Information"
        },
        "submit": {
            "type": "plain_text",
            "text": "Submit",
        },
        "close": {
            "type": "plain_text",
            "text": "Close",
        },
        "blocks": [
            {
                "type": "input",
                "block_id": "tag_block",
                "optional": False,
                "element": {
                    "type": "multi_static_select",
                    "action_id": "tag_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Select one or more tags"
                    },
                    "options": sorted_issues_tags,
                },
                "label": {
                    "type": "plain_text",
                    "text": "Tags"
                }
            },
            {
                "type": "input",
                "block_id": "post_url_block",
                "optional": True,
                "element": {
                    "type": "url_text_input",
                    "action_id": "post_url_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Enter post URL"
                    }
                },
                "label": {
                    "type": "plain_text",
                    "text": "Post URL"
                }
            },
            {
                "type": "input",
                "block_id": "ticket_url_block",
                "optional": True,
                "element": {
                    "type": "url_text_input",
                    "action_id": "ticket_url_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Enter ticket URL"
                    }
                },
                "label": {
                    "type": "plain_text",
                    "text": "Ticket URL"
                }
            },
        ]
    }
