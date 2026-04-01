import os
import hashlib
import logging
import threading
from datetime import datetime, timezone
from functools import wraps

import requests as http_requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, redirect, url_for, make_response
from apscheduler.schedulers.background import BackgroundScheduler

import db
import scraper

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(32)

MONITOR_PASSWORD = os.getenv("MONITOR_PASSWORD", "")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
AUTH_COOKIE = "tiktok_auth"
ALERT_SCAN_INTERVAL = 1

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


@app.route("/admin")
@require_auth
def admin():
    return render_template("admin.html")


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


@app.route("/api/admin/settings", methods=["GET"])
@require_auth
def api_admin_settings():
    settings = db.get_all_settings()
    return jsonify({
        "slack_webhook_url": settings.get("slack_webhook_url", {}).get("value", SLACK_WEBHOOK_URL),
        "slack_default_channel": settings.get("slack_default_channel", {}).get("value", ""),
        "last_alert_run": get_alert_status(),
    })


@app.route("/api/admin/settings", methods=["POST"])
@require_auth
def api_admin_save_settings():
    data = request.get_json(force=True)
    webhook_url = (data.get("slack_webhook_url") or "").strip()
    default_channel = (data.get("slack_default_channel") or "").strip()
    db.set_setting("slack_webhook_url", webhook_url)
    db.set_setting("slack_default_channel", default_channel)
    return jsonify({"ok": True, "slack_webhook_url": webhook_url, "slack_default_channel": default_channel})


def _validate_rule_payload(data):
    name = (data.get("name") or "").strip()
    username = (data.get("username") or "").strip().lstrip("@").lower() or None
    slack_channel = (data.get("slack_channel") or "").strip() or None
    try:
        min_views = int(data.get("min_views"))
        max_post_age_minutes = int(data.get("max_post_age_minutes"))
        check_every_minutes = int(data.get("check_every_minutes"))
    except (TypeError, ValueError):
        return None, "Los campos numéricos son inválidos"

    if not name:
        return None, "El nombre de la regla es obligatorio"
    if min_views < 1:
        return None, "min_views debe ser >= 1"
    if max_post_age_minutes < 1 or max_post_age_minutes > 10080:
        return None, "max_post_age_minutes debe estar entre 1 y 10080"
    if check_every_minutes < 1 or check_every_minutes > 1440:
        return None, "check_every_minutes debe estar entre 1 y 1440"

    return {
        "name": name,
        "username": username,
        "min_views": min_views,
        "max_post_age_minutes": max_post_age_minutes,
        "check_every_minutes": check_every_minutes,
        "slack_channel": slack_channel,
        "enabled": bool(data.get("enabled", True)),
    }, None


@app.route("/api/admin/rules", methods=["GET"])
@require_auth
def api_admin_rules():
    return jsonify(db.list_alert_rules())


@app.route("/api/admin/rules", methods=["POST"])
@require_auth
def api_admin_create_rule():
    payload, error = _validate_rule_payload(request.get_json(force=True))
    if error:
        return jsonify({"error": error}), 400
    return jsonify(db.save_alert_rule(None, payload)), 201


@app.route("/api/admin/rules/<int:rule_id>", methods=["PUT"])
@require_auth
def api_admin_update_rule(rule_id):
    payload, error = _validate_rule_payload(request.get_json(force=True))
    if error:
        return jsonify({"error": error}), 400
    return jsonify(db.save_alert_rule(rule_id, payload))


@app.route("/api/admin/rules/<int:rule_id>", methods=["DELETE"])
@require_auth
def api_admin_delete_rule(rule_id):
    db.delete_alert_rule(rule_id)
    return jsonify({"ok": True})


@app.route("/api/admin/alerts/run", methods=["POST"])
@require_auth
def api_admin_run_alerts():
    return jsonify(run_alert_rules(force=True))


@app.route("/api/admin/alerts/status", methods=["GET"])
@require_auth
def api_admin_alert_status():
    return jsonify(get_alert_status())


# --- Slack Notifications ---

def get_slack_webhook_url():
    return (db.get_setting("slack_webhook_url", SLACK_WEBHOOK_URL) or "").strip()


def get_default_slack_channel():
    return (db.get_setting("slack_default_channel", "") or "").strip()


def send_slack_message(text, channel=None):
    webhook_url = get_slack_webhook_url()
    if not webhook_url:
        return False, "slack webhook no configurado"
    payload = {"text": text}
    channel_name = (channel or get_default_slack_channel() or "").strip()
    if channel_name:
        payload["channel"] = channel_name
    try:
        resp = http_requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code != 200:
            return False, f"slack devolvió {resp.status_code}: {resp.text[:120]}"
        return True, None
    except Exception as e:
        return False, str(e)


def notify_slack(new_posts):
    """Send a Slack message via webhook when new TikTok posts are found."""
    if not new_posts:
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

    ok, error = send_slack_message("\n".join(lines))
    if ok:
        logger.info(f"Slack notification sent ({len(new_posts)} posts)")
    else:
        logger.warning(f"Slack notification failed: {error}")


def _parse_iso_datetime(value):
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _minutes_since_post(post, now_utc):
    post_dt = _parse_iso_datetime(post.get("post_date")) or _parse_iso_datetime(post.get("discovered_at"))
    if not post_dt:
        return None
    return max(0, int((now_utc - post_dt).total_seconds() // 60))


def _format_rule_alert(rule, post, age_minutes):
    desc = (post.get("description") or "Sin descripción").strip()
    if len(desc) > 120:
        desc = desc[:117] + "..."
    return "\n".join([
        f"*Regla disparada:* {rule['name']}",
        f"*Cuenta:* @{post['username']}",
        f"*Views:* {post.get('view_count') or 0}",
        f"*Edad del post:* {age_minutes} min",
        f"*Umbral:* {rule['min_views']} views en <= {rule['max_post_age_minutes']} min",
        f"*Post:* <{post['url']}|Abrir en TikTok>",
        f"*Texto:* {desc}",
    ])


_alert_lock = threading.Lock()
_alert_status = {
    "running": False,
    "last_run_at": None,
    "checked_rules": 0,
    "triggered_rules": 0,
    "notifications_sent": 0,
    "errors": [],
}


def get_alert_status():
    return dict(_alert_status)


def _rule_is_due(rule, now_utc):
    last_checked = _parse_iso_datetime(rule.get("last_checked_at"))
    if not last_checked:
        return True
    elapsed = (now_utc - last_checked).total_seconds() / 60
    return elapsed >= int(rule["check_every_minutes"])


def _evaluate_rule(rule, now_utc):
    posts = db.get_recent_posts_for_alerts(username=rule.get("username"))
    if not posts:
        db.mark_alert_rule_checked(rule["id"], now_utc.isoformat(), matched=False)
        return {"matches": 0, "sent": 0, "errors": []}

    alerted_ids = db.get_alerted_video_ids(rule["id"], [p["video_id"] for p in posts])
    matched = 0
    sent = 0
    errors = []

    for post in posts:
        if post["video_id"] in alerted_ids:
            continue
        view_count = post.get("view_count") or 0
        if view_count < int(rule["min_views"]):
            continue
        age_minutes = _minutes_since_post(post, now_utc)
        if age_minutes is None or age_minutes > int(rule["max_post_age_minutes"]):
            continue
        matched += 1
        ok, error = send_slack_message(_format_rule_alert(rule, post, age_minutes), channel=rule.get("slack_channel"))
        if ok:
            db.record_alert_event(rule["id"], post["video_id"], view_count)
            sent += 1
        else:
            errors.append(f"Regla {rule['id']} @{post['username']} {post['video_id']}: {error}")

    db.mark_alert_rule_checked(rule["id"], now_utc.isoformat(), matched=matched > 0 and sent > 0)
    return {"matches": matched, "sent": sent, "errors": errors}


def run_alert_rules(force=False):
    if not _alert_lock.acquire(blocking=False):
        return {"message": "Alert evaluation already running", **get_alert_status()}

    now_utc = datetime.now(timezone.utc)
    checked_rules = 0
    triggered_rules = 0
    notifications_sent = 0
    errors = []
    _alert_status.update({
        "running": True,
        "last_run_at": now_utc.isoformat(),
        "checked_rules": 0,
        "triggered_rules": 0,
        "notifications_sent": 0,
        "errors": [],
    })
    try:
        rules = db.get_enabled_alert_rules()
        for rule in rules:
            if not force and not _rule_is_due(rule, now_utc):
                continue
            checked_rules += 1
            result = _evaluate_rule(rule, now_utc)
            if result["matches"] > 0:
                triggered_rules += 1
            notifications_sent += result["sent"]
            errors.extend(result["errors"])
        return {
            "last_run_at": now_utc.isoformat(),
            "checked_rules": checked_rules,
            "triggered_rules": triggered_rules,
            "notifications_sent": notifications_sent,
            "errors": errors,
        }
    finally:
        _alert_status.update({
            "running": False,
            "last_run_at": now_utc.isoformat(),
            "checked_rules": checked_rules,
            "triggered_rules": triggered_rules,
            "notifications_sent": notifications_sent,
            "errors": errors,
        })
        _alert_lock.release()


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


# --- Scheduler ---

scheduler = BackgroundScheduler()
scheduler.add_job(run_scan, "interval", minutes=SCAN_INTERVAL, id="tiktok_scan", replace_existing=True)
scheduler.add_job(run_alert_rules, "interval", minutes=ALERT_SCAN_INTERVAL, id="alert_rules", replace_existing=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3457))
    db.init_db()
    scheduler.start()
    logger.info(f"TikTok Monitor starting on :{port} (scan every {SCAN_INTERVAL}min)")
    try:
        app.run(host="0.0.0.0", port=port, debug=False)
    finally:
        scheduler.shutdown()
