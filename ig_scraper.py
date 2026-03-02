import logging
import os
import time
from itertools import islice

import instaloader
from instaloader import Profile

logger = logging.getLogger(__name__)

IG_SESSION_USER = os.getenv("IG_SESSION_USER", "")

# Global rate limit state: if we hit 429, stop all requests until cooldown expires
_rate_limited_until = 0  # unix timestamp
RATE_LIMIT_COOLDOWN = 35 * 60  # 35 min cooldown after hitting 429


class RateLimitError(Exception):
    """Raised when Instagram rate-limits us."""
    pass


# Custom rate controller: abort if instaloader wants to wait > 120s
class _AbortOnLongWait(instaloader.RateController):
    def sleep(self, secs):
        if secs > 120:
            raise RateLimitError(f"Rate limit wait too long ({secs:.0f}s), aborting")
        super().sleep(secs)


def _get_loader():
    """Create and return an authenticated Instaloader instance."""
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        rate_controller=lambda ctx: _AbortOnLongWait(ctx),
    )
    if IG_SESSION_USER:
        try:
            L.load_session_from_file(IG_SESSION_USER)
            logger.info(f"Loaded Instagram session for @{IG_SESSION_USER}")
        except FileNotFoundError:
            logger.error(f"No session file found for @{IG_SESSION_USER}. Run: instaloader --login {IG_SESSION_USER}")
    else:
        logger.warning("IG_SESSION_USER not set — Instagram scraping will likely fail with 429")
    return L


_loader = None


def _get_cached_loader():
    global _loader
    if _loader is None:
        _loader = _get_loader()
    return _loader


def _check_rate_limit():
    """Check if we're in a global rate limit cooldown. Returns remaining seconds or 0."""
    global _rate_limited_until
    now = time.time()
    if now < _rate_limited_until:
        return int(_rate_limited_until - now)
    return 0


def _set_rate_limited():
    """Set global rate limit cooldown."""
    global _rate_limited_until
    _rate_limited_until = time.time() + RATE_LIMIT_COOLDOWN
    mins = RATE_LIMIT_COOLDOWN // 60
    logger.warning(f"IG rate limit hit — backing off for {mins} minutes (until {time.strftime('%H:%M', time.localtime(_rate_limited_until))})")


def scrape_profile(username, max_posts=20):
    """Fetch profile info + recent Reels in a single Profile lookup.

    Returns (posts_list, avatar_url_or_None, error_string_or_None).
    Only one API call for Profile.from_username() per profile.
    Raises RateLimitError if rate-limited (caller should abort remaining profiles).
    """
    L = _get_cached_loader()
    posts = []
    avatar_url = None
    try:
        profile = Profile.from_username(L.context, username)
        avatar_url = profile.profile_pic_url

        posts_seen = 0
        for post in islice(profile.get_posts(), max_posts):
            posts_seen += 1
            if not post.is_video:
                continue
            posts.append({
                "shortcode": post.shortcode,
                "username": username,
                "caption": post.caption or "",
                "url": f"https://www.instagram.com/reel/{post.shortcode}/",
                "post_date": post.date_utc.isoformat() if post.date_utc else None,
                "view_count": post.video_view_count,
                "like_count": post.likes,
                "comment_count": post.comments,
                "thumbnail_url": post.url,  # .url is the display image
            })

        # Warn if likely rate-limited (profile loaded but 0 posts from active account)
        if posts_seen == 0 and profile.mediacount > 0:
            logger.warning(f"IG @{username}: profile has {profile.mediacount} posts but API returned 0 — likely rate-limited")
            return posts, avatar_url, f"@{username}: 0 posts returned (likely rate-limited)"

    except instaloader.exceptions.ProfileNotExistsException:
        logger.warning(f"IG profile @{username} does not exist, skipping")
        return posts, None, f"@{username}: profile does not exist"
    except instaloader.exceptions.PrivateProfileNotFollowedException:
        logger.warning(f"IG profile @{username} is private, skipping")
        return posts, None, f"@{username}: private profile"
    except instaloader.exceptions.LoginRequiredException:
        logger.error(f"Login required to access @{username}")
        return posts, None, f"@{username}: login required"
    except RateLimitError as e:
        _set_rate_limited()
        raise  # propagate so scan_all aborts immediately
    except ConnectionError as e:
        logger.error(f"Connection error for IG @{username}: {e}")
        return posts, None, f"@{username}: {e}"
    except Exception as e:
        logger.error(f"Error scraping IG @{username}: {e}")
        return posts, None, f"@{username}: {e}"

    return posts, avatar_url, None


def get_avatar_url(username):
    """Fetch avatar URL for an Instagram username (standalone, used on account add)."""
    remaining = _check_rate_limit()
    if remaining:
        logger.warning(f"IG rate-limited, skipping avatar fetch for @{username} ({remaining}s remaining)")
        return None
    L = _get_cached_loader()
    try:
        profile = Profile.from_username(L.context, username)
        return profile.profile_pic_url
    except RateLimitError:
        _set_rate_limited()
    except Exception as e:
        logger.warning(f"Could not fetch IG avatar for @{username}: {e}")
    return None


def scan_all_ig_accounts(accounts, need_avatar=None):
    """Scan a list of IG accounts. Returns (all_posts, avatars, errors).

    Uses a single Profile lookup per account (reels + avatar combined).
    Aborts immediately on rate limit to avoid extending the cooldown.
    """
    all_posts = []
    avatars = {}
    errors = []
    if need_avatar is None:
        need_avatar = set()

    # Check global cooldown before starting
    remaining = _check_rate_limit()
    if remaining:
        mins = remaining // 60
        msg = f"IG rate-limited, skipping scan ({mins}m remaining)"
        logger.warning(msg)
        errors.append(msg)
        return all_posts, avatars, errors

    for i, username in enumerate(accounts):
        logger.info(f"Scanning IG @{username} ({i+1}/{len(accounts)})")

        try:
            posts, avatar_url, error = scrape_profile(username)
        except RateLimitError:
            # Abort the entire scan — don't try more profiles
            remaining_accts = len(accounts) - i - 1
            msg = f"IG rate limit hit on @{username}, aborting scan ({remaining_accts} accounts skipped)"
            logger.warning(msg)
            errors.append(msg)
            break

        all_posts.extend(posts)
        logger.info(f"  Found {len(posts)} reels for IG @{username}")

        if error:
            errors.append(error)

        if avatar_url and username in need_avatar:
            avatars[username] = avatar_url
            logger.info(f"  Got IG avatar for @{username}")

        # Sleep 10s between profiles to avoid rate limiting
        if i < len(accounts) - 1:
            time.sleep(10)

    return all_posts, avatars, errors
