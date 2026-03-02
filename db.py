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

        CREATE TABLE IF NOT EXISTS ig_accounts (
            username TEXT PRIMARY KEY,
            added_at TEXT DEFAULT (datetime('now')),
            active INTEGER DEFAULT 1,
            avatar_url TEXT
        );

        CREATE TABLE IF NOT EXISTS ig_posts (
            shortcode TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            caption TEXT,
            url TEXT NOT NULL,
            post_date TEXT,
            discovered_at TEXT DEFAULT (datetime('now')),
            view_count INTEGER,
            like_count INTEGER,
            comment_count INTEGER,
            thumbnail_url TEXT,
            FOREIGN KEY (username) REFERENCES ig_accounts(username) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS ig_scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            finished_at TEXT,
            new_posts INTEGER DEFAULT 0,
            errors TEXT
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
    """Upsert posts (insert new, update stats on existing). Returns count of newly inserted."""
    if not posts:
        return 0
    conn = _connect()
    cursor = conn.cursor()
    # Get existing IDs to distinguish new vs updated
    ids = [p["video_id"] for p in posts]
    placeholders = ",".join("?" * len(ids))
    existing = {r[0] for r in cursor.execute(f"SELECT video_id FROM posts WHERE video_id IN ({placeholders})", ids).fetchall()}
    new_count = 0
    for p in posts:
        cursor.execute(
            """INSERT INTO posts (video_id, username, description, url, post_date, view_count, like_count, comment_count, repost_count, thumbnail_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(video_id) DO UPDATE SET
                   view_count = excluded.view_count,
                   like_count = excluded.like_count,
                   comment_count = excluded.comment_count,
                   repost_count = excluded.repost_count,
                   thumbnail_url = excluded.thumbnail_url""",
            (p["video_id"], p["username"], p.get("description", ""), p["url"], p.get("post_date"),
             p.get("view_count"), p.get("like_count"), p.get("comment_count"), p.get("repost_count"),
             p.get("thumbnail_url")),
        )
        if p["video_id"] not in existing:
            new_count += 1
    conn.commit()
    conn.close()
    return new_count


def update_avatar(username, avatar_url):
    conn = _connect()
    conn.execute("UPDATE accounts SET avatar_url = ? WHERE username = ?", (avatar_url, username))
    conn.commit()
    conn.close()


def get_posts(username=None, limit=50, offset=0):
    conn = _connect()
    order = "ORDER BY COALESCE(post_date, discovered_at) DESC"
    if username:
        rows = conn.execute(
            f"SELECT * FROM posts WHERE username = ? {order} LIMIT ? OFFSET ?",
            (username, limit, offset),
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


# ── Instagram ────────────────────────────────────────────

def get_active_ig_accounts():
    conn = _connect()
    rows = conn.execute("SELECT username, avatar_url FROM ig_accounts WHERE active = 1 ORDER BY added_at").fetchall()
    conn.close()
    return [{"username": r["username"], "avatar_url": r["avatar_url"]} for r in rows]


def add_ig_account(username):
    username = username.strip().lstrip("@").lower()
    if not username:
        return None
    conn = _connect()
    conn.execute("INSERT OR IGNORE INTO ig_accounts (username) VALUES (?)", (username,))
    conn.commit()
    conn.close()
    return username


def remove_ig_account(username):
    conn = _connect()
    conn.execute("DELETE FROM ig_accounts WHERE username = ?", (username,))
    conn.commit()
    conn.close()


def get_known_ig_shortcodes(username):
    conn = _connect()
    rows = conn.execute("SELECT shortcode FROM ig_posts WHERE username = ?", (username,)).fetchall()
    conn.close()
    return {r["shortcode"] for r in rows}


def insert_ig_posts(posts):
    """Upsert IG posts (insert new, update stats on existing). Returns count of newly inserted."""
    if not posts:
        return 0
    conn = _connect()
    cursor = conn.cursor()
    ids = [p["shortcode"] for p in posts]
    placeholders = ",".join("?" * len(ids))
    existing = {r[0] for r in cursor.execute(f"SELECT shortcode FROM ig_posts WHERE shortcode IN ({placeholders})", ids).fetchall()}
    new_count = 0
    for p in posts:
        cursor.execute(
            """INSERT INTO ig_posts (shortcode, username, caption, url, post_date, view_count, like_count, comment_count, thumbnail_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(shortcode) DO UPDATE SET
                   view_count = excluded.view_count,
                   like_count = excluded.like_count,
                   comment_count = excluded.comment_count,
                   thumbnail_url = excluded.thumbnail_url""",
            (p["shortcode"], p["username"], p.get("caption", ""), p["url"], p.get("post_date"),
             p.get("view_count"), p.get("like_count"), p.get("comment_count"),
             p.get("thumbnail_url")),
        )
        if p["shortcode"] not in existing:
            new_count += 1
    conn.commit()
    conn.close()
    return new_count


def update_ig_avatar(username, avatar_url):
    conn = _connect()
    conn.execute("UPDATE ig_accounts SET avatar_url = ? WHERE username = ?", (avatar_url, username))
    conn.commit()
    conn.close()


def get_ig_posts(username=None, limit=50, offset=0):
    conn = _connect()
    order = "ORDER BY COALESCE(post_date, discovered_at) DESC"
    if username:
        rows = conn.execute(
            f"SELECT * FROM ig_posts WHERE username = ? {order} LIMIT ? OFFSET ?",
            (username, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM ig_posts {order} LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_ig_scan_log(started_at, finished_at, new_posts, errors=None):
    conn = _connect()
    conn.execute(
        "INSERT INTO ig_scan_log (started_at, finished_at, new_posts, errors) VALUES (?, ?, ?, ?)",
        (started_at, finished_at, new_posts, errors),
    )
    conn.commit()
    conn.close()


def purge_old_ig_posts(hours=24):
    """Delete IG posts older than `hours` hours. Returns count deleted."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM ig_posts WHERE discovered_at < datetime('now', ?)",
        (f"-{hours} hours",),
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def get_last_ig_scan():
    conn = _connect()
    row = conn.execute("SELECT * FROM ig_scan_log ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None
