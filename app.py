import os
import hashlib
import logging
import threading
from datetime import datetime
from functools import wraps

import requests as http_requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, redirect, url_for, make_response
from apscheduler.schedulers.background import BackgroundScheduler

import db
import scraper
import ig_scraper

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(32)

MONITOR_PASSWORD = os.getenv("MONITOR_PASSWORD", "")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))
IG_SCAN_INTERVAL = int(os.getenv("IG_SCAN_INTERVAL_MINUTES", "30"))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
AUTH_COOKIE = "tiktok_auth"

# Auth token: deterministic hash so it survives restarts
_AUTH_TOKEN = hashlib.sha256(MONITOR_PASSWORD.encode()).hexdigest() if MONITOR_PASSWORD else ""

# --- Auth ---

def _check_auth():
    if not MONITOR_PASSWORD:
        return True
    token = request.cookies.get(AUTH_COOKIE, "")
    return token == _AUTH_TOKEN


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _check_auth():
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET"])
def login_page():
    if not MONITOR_PASSWORD or _check_auth():
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login_submit():
    password = request.form.get("password", "")
    if password == MONITOR_PASSWORD:
        resp = make_response(redirect(url_for("dashboard")))
        resp.set_cookie(AUTH_COOKIE, _AUTH_TOKEN, httponly=True, samesite="Lax", max_age=86400 * 30)
        return resp
    return render_template("login.html", error="Password incorrecto"), 401


@app.route("/logout")
def logout():
    resp = make_response(redirect(url_for("login_page")))
    resp.delete_cookie(AUTH_COOKIE)
    return resp


# --- Dashboard ---

@app.route("/")
@require_auth
def dashboard():
    return render_template("index.html")


@app.route("/docs")
@require_auth
def docs():
    return render_template("docs.html")


# --- API ---

@app.route("/api/accounts", methods=["GET"])
@require_auth
def api_accounts():
    return jsonify(db.get_active_accounts())


@app.route("/api/accounts", methods=["POST"])
@require_auth
def api_add_account():
    data = request.get_json(force=True)
    username = data.get("username", "")
    result = db.add_account(username)
    if not result:
        return jsonify({"error": "username vacío"}), 400
    # Try to fetch avatar immediately
    avatar = scraper.get_avatar_url(result)
    if avatar:
        db.update_avatar(result, avatar)
    return jsonify({"username": result, "avatar_url": avatar}), 201


@app.route("/api/accounts/<username>", methods=["DELETE"])
@require_auth
def api_remove_account(username):
    db.remove_account(username)
    return jsonify({"ok": True})


@app.route("/api/posts", methods=["GET"])
@require_auth
def api_posts():
    usernames = request.args.getlist("username")
    limit = int(request.args.get("limit", "50"))
    offset = int(request.args.get("offset", "0"))
    posts = db.get_posts(usernames=usernames or None, limit=limit, offset=offset)
    return jsonify(posts)


@app.route("/api/posts/counts", methods=["GET"])
@require_auth
def api_post_counts():
    counts, total = db.get_post_counts()
    return jsonify({"counts": counts, "total": total})


@app.route("/api/scan", methods=["POST"])
@require_auth
def api_scan():
    result = run_scan()
    return jsonify(result)


@app.route("/api/status", methods=["GET"])
@require_auth
def api_status():
    last_scan = db.get_last_scan()
    job = scheduler.get_job("tiktok_scan")
    next_run = str(job.next_run_time) if job and job.next_run_time else None
    interval = int(job.trigger.interval.total_seconds() // 60) if job else SCAN_INTERVAL
    paused = job.next_run_time is None if job else True
    return jsonify({
        "last_scan": last_scan,
        "next_scan": next_run,
        "interval_minutes": interval,
        "auto_scan": not paused,
        "scan_progress": get_scan_progress(),
    })


@app.route("/api/autoscan", methods=["POST"])
@require_auth
def api_toggle_autoscan():
    data = request.get_json(force=True)
    enabled = data.get("enabled", True)
    job = scheduler.get_job("tiktok_scan")
    if not job:
        return jsonify({"error": "no job found"}), 500
    if enabled:
        job.resume()
        logger.info("Auto-scan enabled")
    else:
        job.pause()
        logger.info("Auto-scan paused")
    return jsonify({"auto_scan": enabled})


@app.route("/api/interval", methods=["POST"])
@require_auth
def api_set_interval():
    data = request.get_json(force=True)
    minutes = data.get("minutes")
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return jsonify({"error": "valor invalido"}), 400
    if minutes < 1 or minutes > 1440:
        return jsonify({"error": "rango: 1-1440 minutos"}), 400
    scheduler.reschedule_job("tiktok_scan", trigger="interval", minutes=minutes)
    logger.info(f"Scan interval changed to {minutes}min")
    return jsonify({"interval_minutes": minutes})


# --- Instagram API ---

@app.route("/api/ig/accounts", methods=["GET"])
@require_auth
def api_ig_accounts():
    return jsonify(db.get_active_ig_accounts())


@app.route("/api/ig/accounts", methods=["POST"])
@require_auth
def api_add_ig_account():
    data = request.get_json(force=True)
    username = data.get("username", "")
    result = db.add_ig_account(username)
    if not result:
        return jsonify({"error": "username vacío"}), 400
    # Don't fetch avatar on add — IG rate-limits aggressively.
    # Avatar will be fetched during the next scan.
    return jsonify({"username": result, "avatar_url": None}), 201


@app.route("/api/ig/accounts/<username>", methods=["DELETE"])
@require_auth
def api_remove_ig_account(username):
    db.remove_ig_account(username)
    return jsonify({"ok": True})


@app.route("/api/ig/posts", methods=["GET"])
@require_auth
def api_ig_posts():
    username = request.args.get("username")
    limit = int(request.args.get("limit", "50"))
    offset = int(request.args.get("offset", "0"))
    posts = db.get_ig_posts(username=username, limit=limit, offset=offset)
    return jsonify(posts)


@app.route("/api/ig/scan", methods=["POST"])
@require_auth
def api_ig_scan():
    result = run_ig_scan()
    return jsonify(result)


@app.route("/api/ig/status", methods=["GET"])
@require_auth
def api_ig_status():
    last_scan = db.get_last_ig_scan()
    job = scheduler.get_job("instagram_scan")
    next_run = str(job.next_run_time) if job and job.next_run_time else None
    interval = int(job.trigger.interval.total_seconds() // 60) if job else IG_SCAN_INTERVAL
    paused = job.next_run_time is None if job else True
    return jsonify({
        "last_scan": last_scan,
        "next_scan": next_run,
        "interval_minutes": interval,
        "auto_scan": not paused,
    })


@app.route("/api/ig/autoscan", methods=["POST"])
@require_auth
def api_ig_toggle_autoscan():
    data = request.get_json(force=True)
    enabled = data.get("enabled", True)
    job = scheduler.get_job("instagram_scan")
    if not job:
        return jsonify({"error": "no job found"}), 500
    if enabled:
        job.resume()
        logger.info("IG auto-scan enabled")
    else:
        job.pause()
        logger.info("IG auto-scan paused")
    return jsonify({"auto_scan": enabled})


@app.route("/api/ig/interval", methods=["POST"])
@require_auth
def api_ig_set_interval():
    data = request.get_json(force=True)
    minutes = data.get("minutes")
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return jsonify({"error": "valor invalido"}), 400
    if minutes < 1 or minutes > 1440:
        return jsonify({"error": "rango: 1-1440 minutos"}), 400
    scheduler.reschedule_job("instagram_scan", trigger="interval", minutes=minutes)
    logger.info(f"IG scan interval changed to {minutes}min")
    return jsonify({"interval_minutes": minutes})


# --- Slack Notifications ---

def notify_slack(new_posts):
    """Send a Slack message via webhook when new TikTok posts are found."""
    if not SLACK_WEBHOOK_URL or not new_posts:
        return
    # Group by username
    by_user = {}
    for p in new_posts:
        by_user.setdefault(p["username"], []).append(p)

    lines = [f"*🔔 {len(new_posts)} nuevo{'s' if len(new_posts) != 1 else ''} post{'s' if len(new_posts) != 1 else ''} en TikTok*\n"]
    for username, posts in by_user.items():
        lines.append(f"*@{username}* — {len(posts)} post{'s' if len(posts) != 1 else ''}:")
        for p in posts[:5]:  # max 5 per user to avoid huge messages
            desc = (p.get("description") or "Sin descripción")[:80]
            lines.append(f"  • <{p['url']}|{desc}>")
        if len(posts) > 5:
            lines.append(f"  _...y {len(posts) - 5} más_")

    payload = {"text": "\n".join(lines)}
    try:
        resp = http_requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"Slack webhook returned {resp.status_code}: {resp.text[:100]}")
        else:
            logger.info(f"Slack notification sent ({len(new_posts)} posts)")
    except Exception as e:
        logger.warning(f"Slack notification failed: {e}")


# --- Scan Logic ---

_scan_lock = threading.Lock()
_scan_progress = {"running": False, "current": 0, "total": 0, "current_user": "", "started_at": None}


def get_scan_progress():
    return dict(_scan_progress)


def run_scan():
    if not _scan_lock.acquire(blocking=False):
        return {"new_posts": 0, "errors": [], "message": "Scan already running", "progress": get_scan_progress()}
    try:
        return _run_scan_inner()
    finally:
        _scan_progress["running"] = False
        _scan_lock.release()


def _on_scan_progress(current, total, username):
    _scan_progress.update({"current": current, "total": total, "current_user": username})


def _run_scan_inner():
    accounts = db.get_active_accounts()
    if not accounts:
        return {"new_posts": 0, "errors": [], "message": "No hay cuentas para escanear"}

    usernames = [a["username"] for a in accounts]
    need_avatar = {a["username"] for a in accounts if not a["avatar_url"]}

    started_at = datetime.utcnow().isoformat()
    _scan_progress.update({"running": True, "current": 0, "total": len(usernames), "current_user": "", "started_at": started_at})
    logger.info(f"Scan started: {len(usernames)} accounts")

    all_posts, avatars, errors = scraper.scan_all_accounts(usernames, need_avatar=need_avatar, on_progress=_on_scan_progress)

    # Save avatars
    for username, avatar_url in avatars.items():
        db.update_avatar(username, avatar_url)

    new_count, new_posts = db.insert_posts(all_posts)
    purged = db.purge_old_posts(hours=24)
    finished_at = datetime.utcnow().isoformat()

    db.add_scan_log(started_at, finished_at, new_count, "; ".join(errors) if errors else None)
    logger.info(f"Scan finished: {new_count} new, {purged} purged, {len(errors)} errors")

    # Notify Slack about new posts
    notify_slack(new_posts)

    return {
        "new_posts": new_count,
        "total_fetched": len(all_posts),
        "errors": errors,
        "started_at": started_at,
        "finished_at": finished_at,
    }


def run_ig_scan():
    accounts = db.get_active_ig_accounts()
    if not accounts:
        return {"new_posts": 0, "errors": [], "message": "No hay cuentas IG para escanear"}

    usernames = [a["username"] for a in accounts]
    need_avatar = {a["username"] for a in accounts if not a["avatar_url"]}

    started_at = datetime.utcnow().isoformat()
    logger.info(f"IG scan started: {len(usernames)} accounts")

    all_posts, avatars, errors = ig_scraper.scan_all_ig_accounts(usernames, need_avatar=need_avatar)

    for username, avatar_url in avatars.items():
        db.update_ig_avatar(username, avatar_url)

    new_count = db.insert_ig_posts(all_posts)
    purged = db.purge_old_ig_posts(hours=24)
    finished_at = datetime.utcnow().isoformat()

    db.add_ig_scan_log(started_at, finished_at, new_count, "; ".join(errors) if errors else None)
    logger.info(f"IG scan finished: {new_count} new, {purged} purged, {len(errors)} errors")

    return {
        "new_posts": new_count,
        "total_fetched": len(all_posts),
        "errors": errors,
        "started_at": started_at,
        "finished_at": finished_at,
    }


# --- Scheduler ---

scheduler = BackgroundScheduler()
scheduler.add_job(run_scan, "interval", minutes=SCAN_INTERVAL, id="tiktok_scan", replace_existing=True)
# IG scan disabled — Instagram rate-limits too aggressively for viable monitoring
# scheduler.add_job(run_ig_scan, "interval", minutes=IG_SCAN_INTERVAL, id="instagram_scan", replace_existing=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3457))
    db.init_db()
    scheduler.start()
    logger.info(f"TikTok Monitor starting on :{port} (scan every {SCAN_INTERVAL}min)")
    try:
        app.run(host="0.0.0.0", port=port, debug=False)
    finally:
        scheduler.shutdown()
