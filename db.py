import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tiktok.db")


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            username TEXT PRIMARY KEY,
            added_at TEXT DEFAULT (datetime('now')),
            active INTEGER DEFAULT 1,
            avatar_url TEXT
        );

        CREATE TABLE IF NOT EXISTS posts (
            video_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            description TEXT,
            url TEXT NOT NULL,
            post_date TEXT,
            discovered_at TEXT DEFAULT (datetime('now')),
            view_count INTEGER,
            like_count INTEGER,
            comment_count INTEGER,
            repost_count INTEGER,
            thumbnail_url TEXT,
            FOREIGN KEY (username) REFERENCES accounts(username) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            finished_at TEXT,
            new_posts INTEGER DEFAULT 0,
            errors TEXT
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS alert_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            username TEXT,
            min_views INTEGER NOT NULL,
            max_post_age_minutes INTEGER NOT NULL,
            check_every_minutes INTEGER NOT NULL,
            slack_channel TEXT,
            enabled INTEGER DEFAULT 1,
            last_checked_at TEXT,
            last_matched_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS alert_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            video_id TEXT NOT NULL,
            view_count INTEGER,
            notified_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (rule_id) REFERENCES alert_rules(id) ON DELETE CASCADE,
            UNIQUE(rule_id, video_id)
        );
    """)
    # Migrate existing DBs — add columns if missing
    migrations = [
        ("accounts", "avatar_url", "TEXT"),
        ("posts", "view_count", "INTEGER"),
        ("posts", "like_count", "INTEGER"),
        ("posts", "comment_count", "INTEGER"),
        ("posts", "repost_count", "INTEGER"),
        ("posts", "thumbnail_url", "TEXT"),
    ]
    for table, col, col_type in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.close()


def get_active_accounts():
    conn = _connect()
    rows = conn.execute("SELECT username, avatar_url FROM accounts WHERE active = 1 ORDER BY added_at").fetchall()
    conn.close()
    return [{"username": r["username"], "avatar_url": r["avatar_url"]} for r in rows]


def get_post_counts():
    """Return {username: count} and total count, using SQL GROUP BY."""
    conn = _connect()
    rows = conn.execute("SELECT username, COUNT(*) as cnt FROM posts GROUP BY username").fetchall()
    total_row = conn.execute("SELECT COUNT(*) as cnt FROM posts").fetchone()
    conn.close()
    counts = {r["username"]: r["cnt"] for r in rows}
    return counts, total_row["cnt"]


def add_account(username):
    username = username.strip().lstrip("@").lower()
    if not username:
        return None
    conn = _connect()
    conn.execute("INSERT OR IGNORE INTO accounts (username) VALUES (?)", (username,))
    conn.commit()
    conn.close()
    return username


def remove_account(username):
    conn = _connect()
    conn.execute("DELETE FROM accounts WHERE username = ?", (username,))
    conn.commit()
    conn.close()


def get_known_video_ids(username):
    conn = _connect()
    rows = conn.execute("SELECT video_id FROM posts WHERE username = ?", (username,)).fetchall()
    conn.close()
    return {r["video_id"] for r in rows}


def insert_posts(posts):
    """Upsert posts (insert new, update stats on existing). Returns (count, list) of newly inserted."""
    if not posts:
        return 0, []
    conn = _connect()
    cursor = conn.cursor()
    # Get existing IDs to distinguish new vs updated
    ids = [p["video_id"] for p in posts]
    placeholders = ",".join("?" * len(ids))
    existing = {r[0] for r in cursor.execute(f"SELECT video_id FROM posts WHERE video_id IN ({placeholders})", ids).fetchall()}
    new_count = 0
    new_posts = []
    for p in posts:
        cursor.execute(
            """INSERT INTO posts (video_id, username, description, url, post_date, view_count, like_count, comment_count, repost_count, thumbnail_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(video_id) DO UPDATE SET
                   view_count = excluded.view_count,
                   like_count = excluded.like_count,
                   comment_count = excluded.comment_count,
                   repost_count = excluded.repost_count,
                   thumbnail_url = excluded.thumbnail_url,
                   discovered_at = datetime('now')""",
            (p["video_id"], p["username"], p.get("description", ""), p["url"], p.get("post_date"),
             p.get("view_count"), p.get("like_count"), p.get("comment_count"), p.get("repost_count"),
             p.get("thumbnail_url")),
        )
        if p["video_id"] not in existing:
            new_count += 1
            new_posts.append(p)
    conn.commit()
    conn.close()
    return new_count, new_posts


def update_avatar(username, avatar_url):
    conn = _connect()
    conn.execute("UPDATE accounts SET avatar_url = ? WHERE username = ?", (avatar_url, username))
    conn.commit()
    conn.close()


def get_posts(usernames=None, limit=50, offset=0):
    conn = _connect()
    order = "ORDER BY COALESCE(post_date, discovered_at) DESC"
    if usernames:
        placeholders = ",".join("?" * len(usernames))
        rows = conn.execute(
            f"SELECT * FROM posts WHERE username IN ({placeholders}) {order} LIMIT ? OFFSET ?",
            (*usernames, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM posts {order} LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_scan_log(started_at, finished_at, new_posts, errors=None):
    conn = _connect()
    conn.execute(
        "INSERT INTO scan_log (started_at, finished_at, new_posts, errors) VALUES (?, ?, ?, ?)",
        (started_at, finished_at, new_posts, errors),
    )
    conn.commit()
    conn.close()


def purge_old_posts(hours=24):
    """Delete posts older than `hours` hours. Returns count deleted."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM posts WHERE discovered_at < datetime('now', ?)",
        (f"-{hours} hours",),
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def get_last_scan():
    conn = _connect()
    row = conn.execute("SELECT * FROM scan_log ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None


def get_setting(key, default=None):
    conn = _connect()
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = _connect()
    conn.execute(
        """INSERT INTO app_settings (key, value, updated_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET
               value = excluded.value,
               updated_at = excluded.updated_at""",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_all_settings():
    conn = _connect()
    rows = conn.execute("SELECT key, value, updated_at FROM app_settings ORDER BY key").fetchall()
    conn.close()
    return {r["key"]: {"value": r["value"], "updated_at": r["updated_at"]} for r in rows}


def list_alert_rules():
    conn = _connect()
    rows = conn.execute("SELECT * FROM alert_rules ORDER BY created_at DESC, id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_enabled_alert_rules():
    conn = _connect()
    rows = conn.execute("SELECT * FROM alert_rules WHERE enabled = 1 ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_alert_rule(rule_id, payload):
    conn = _connect()
    cursor = conn.cursor()
    data = (
        payload["name"],
        payload.get("username"),
        payload["min_views"],
        payload["max_post_age_minutes"],
        payload["check_every_minutes"],
        payload.get("slack_channel"),
        1 if payload.get("enabled", True) else 0,
    )
    if rule_id is None:
        cursor.execute(
            """INSERT INTO alert_rules (
                   name, username, min_views, max_post_age_minutes, check_every_minutes,
                   slack_channel, enabled, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            data,
        )
        new_id = cursor.lastrowid
    else:
        cursor.execute(
            """UPDATE alert_rules
               SET name = ?,
                   username = ?,
                   min_views = ?,
                   max_post_age_minutes = ?,
                   check_every_minutes = ?,
                   slack_channel = ?,
                   enabled = ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (*data, rule_id),
        )
        new_id = rule_id
    conn.commit()
    row = conn.execute("SELECT * FROM alert_rules WHERE id = ?", (new_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_alert_rule(rule_id):
    conn = _connect()
    conn.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
    conn.commit()
    conn.close()


def mark_alert_rule_checked(rule_id, checked_at, matched=False):
    conn = _connect()
    if matched:
        conn.execute(
            "UPDATE alert_rules SET last_checked_at = ?, last_matched_at = ?, updated_at = datetime('now') WHERE id = ?",
            (checked_at, checked_at, rule_id),
        )
    else:
        conn.execute(
            "UPDATE alert_rules SET last_checked_at = ?, updated_at = datetime('now') WHERE id = ?",
            (checked_at, rule_id),
        )
    conn.commit()
    conn.close()


def get_recent_posts_for_alerts(username=None, limit=300):
    conn = _connect()
    order = "ORDER BY COALESCE(post_date, discovered_at) DESC"
    if username:
        rows = conn.execute(
            f"SELECT * FROM posts WHERE username = ? {order} LIMIT ?",
            (username, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM posts {order} LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_alerted_video_ids(rule_id, video_ids):
    if not video_ids:
        return set()
    conn = _connect()
    placeholders = ",".join("?" * len(video_ids))
    rows = conn.execute(
        f"SELECT video_id FROM alert_events WHERE rule_id = ? AND video_id IN ({placeholders})",
        (rule_id, *video_ids),
    ).fetchall()
    conn.close()
    return {r["video_id"] for r in rows}


def record_alert_event(rule_id, video_id, view_count):
    conn = _connect()
    conn.execute(
        """INSERT OR IGNORE INTO alert_events (rule_id, video_id, view_count, notified_at)
           VALUES (?, ?, ?, datetime('now'))""",
        (rule_id, video_id, view_count),
    )
    conn.commit()
    conn.close()
