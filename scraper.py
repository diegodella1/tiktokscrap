import json
import logging
import re
import time

import requests
import yt_dlp

logger = logging.getLogger(__name__)


def get_recent_posts(username, max_posts=30):
    """Fetch recent posts metadata for a TikTok username using yt-dlp."""
    url = f"https://www.tiktok.com/@{username}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlist_items": f"1-{max_posts}",
        "skip_download": True,
        "cachedir": False,
        "http_headers": {
            "Cache-Control": "no-cache, no-store",
            "Pragma": "no-cache",
        },
    }

    posts = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(url, download=False)
            if not result:
                return posts

            entries = result.get("entries", [])
            if not entries:
                return posts

            for entry in entries:
                if not entry:
                    continue
                video_id = entry.get("id", "")
                if not video_id:
                    continue
                # description has full text, title is truncated
                desc = entry.get("description") or entry.get("title") or ""
                # timestamp is unix epoch, convert to ISO
                ts = entry.get("timestamp")
                post_date = None
                if ts:
                    from datetime import datetime, timezone
                    post_date = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                thumbs = entry.get("thumbnails") or []
                thumb_url = thumbs[0]["url"] if thumbs else None
                posts.append({
                    "video_id": str(video_id),
                    "username": username,
                    "description": desc,
                    "url": f"https://www.tiktok.com/@{username}/video/{video_id}",
                    "post_date": post_date,
                    "view_count": entry.get("view_count"),
                    "like_count": entry.get("like_count"),
                    "comment_count": entry.get("comment_count"),
                    "repost_count": entry.get("repost_count"),
                    "thumbnail_url": thumb_url,
                })
    except Exception as e:
        logger.error(f"Error scraping @{username}: {e}")

    return posts


def get_avatar_url(username):
    """Fetch avatar URL from TikTok profile HTML."""
    url = f"https://www.tiktok.com/@{username}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        # Parse __UNIVERSAL_DATA_FOR_REHYDRATION__ script tag
        match = re.search(
            r'<script\s+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
            resp.text, re.DOTALL,
        )
        if not match:
            return None
        data = json.loads(match.group(1))
        # Primary path
        try:
            user_detail = data["__DEFAULT_SCOPE__"]["webapp.user-detail"]
            return user_detail["userInfo"]["user"]["avatarThumb"]
        except (KeyError, TypeError):
            pass
        # Legacy fallback
        try:
            return data["UserModule"]["users"][username]["avatarThumb"]
        except (KeyError, TypeError):
            pass
    except Exception as e:
        logger.warning(f"Could not fetch avatar for @{username}: {e}")
    return None


def scan_all_accounts(accounts, need_avatar=None, on_progress=None):
    """Scan a list of accounts, return (all_new_posts, avatars, errors).

    accounts: list of usernames
    need_avatar: set of usernames that need avatar fetching (optional)
    on_progress: callback(current, total, username) called before each account
    """
    all_posts = []
    avatars = {}
    errors = []
    if need_avatar is None:
        need_avatar = set()

    for i, username in enumerate(accounts):
        try:
            logger.info(f"Scanning @{username} ({i+1}/{len(accounts)})")
            if on_progress:
                on_progress(i + 1, len(accounts), username)
            posts = get_recent_posts(username)
            all_posts.extend(posts)
            logger.info(f"  Found {len(posts)} posts for @{username}")
        except Exception as e:
            error_msg = f"@{username}: {e}"
            logger.error(error_msg)
            errors.append(error_msg)

        # Fetch avatar if needed
        if username in need_avatar:
            avatar = get_avatar_url(username)
            if avatar:
                avatars[username] = avatar
                logger.info(f"  Got avatar for @{username}")

        # Sleep between accounts to avoid rate limiting
        if i < len(accounts) - 1:
            time.sleep(3)

    return all_posts, avatars, errors
