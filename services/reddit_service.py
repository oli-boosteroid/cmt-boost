import praw
import os
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


def get_reddit_posts_count(subreddit, start_time, end_time):
    """
    Args:
        subreddit: Subreddit name without /r/ prefix
        start_time: Start period (datetime object)
        end_time: End period (datetime object)

    Returns:
        dict: {
            'count': posts count,
            'posts': list of posts with metadata
        }
    """
    try:
        reddit = praw.Reddit(
            client_id=os.getenv('REDDIT_CLIENT_ID'),
            client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
            user_agent=os.getenv('REDDIT_USER_AGENT'),
            username=os.getenv('REDDIT_USERNAME'),
            password=os.getenv('REDDIT_PASSWORD'),
        )

        posts_in_range = []
        start_ts = int(start_time.timestamp())
        end_ts = int(end_time.timestamp())

        logger.info(f"[Reddit] Looking for posts from {start_time} to {end_time}")
        logger.info(f"[Reddit] Timestamps: {start_ts} - {end_ts}")

        subreddit_obj = reddit.subreddit(subreddit)

        for post in subreddit_obj.new(limit=None):
            created_utc = int(post.created_utc)

            if created_utc < start_ts:
                logger.info(f"[Reddit] Found posts older than {start_time}. Stop searching.")
                break

            if start_ts <= created_utc <= end_ts:
                posts_in_range.append({
                    'id': post.id,
                    'title': post.title,
                    'author': str(post.author) if post.author else '[deleted]',
                    'created_utc': created_utc,
                    'created_readable': datetime.fromtimestamp(created_utc).strftime('%Y-%m-%d %H:%M:%S'),
                    'url': f"https://www.reddit.com{post.permalink}",
                    'score': post.score,
                })

            if len(posts_in_range) % 50 == 0 and len(posts_in_range) > 0:
                logger.info(f"[Reddit] Found posts in the range: {len(posts_in_range)}, continue...")

        logger.info(f"[Reddit] Total found posts in range: {len(posts_in_range)}")

        return {
            'count': len(posts_in_range),
            'posts': posts_in_range
        }

    except Exception as e:
        logger.error(f"[Reddit] Error while getting daily Reddit posts: {e}")
        return {
            'count': 0,
            'posts': []
        }


def _snapshot_posts_count(start_time, end_time):
    """Posts in the console snapshot (filled by the Devvit app) created inside the window."""
    from datetime import timezone
    from services.reddit_snapshot import posts_col
    lo = start_time.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
    hi = end_time.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
    return {"count": posts_col.count_documents({"created": {"$gte": lo, "$lte": hi}})}


def get_daily_window_posts(subreddit):
    """
    Posts in the daily report window: 07:00 yesterday -> 06:59:59 today, KYIV time.
    Uses the same window as the Facebook / Discord / sessions sections. Built in Kyiv time, not the server
    clock (UTC): otherwise the 07:00 Kyiv run would see "before 7 am" and count the day before.
    """
    from services.mongo_db_service import _daily_window
    start_time, end_time = _daily_window()

    from services import reddit_gate
    if reddit_gate.is_down():
        result = _snapshot_posts_count(start_time, end_time)   # API blocked: count what Devvit delivered
    else:
        result = get_reddit_posts_count(subreddit, start_time, end_time)

    logger.info("[Reddit] \n=== Result ===")
    logger.info(f"[Reddit] Range: {start_time} -> {end_time}")
    logger.info(f"[Reddit] Found posts: {result['count']}")

    return result


if __name__ == "__main__":
    # From 7:00 to 06:59 yesterday
    get_daily_window_posts('BoosteroidCommunity')
