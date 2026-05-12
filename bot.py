"""
Bot Telegram — Tracking VA Instagram + GetMySocial + Supabase (Lot 2)
---------------------------------------------------------------------
Rapports automatiques :
    - 00h00 FR : Clics jour J-1 complet
    - 09h30 FR : Insta MATIN (vérif post 07h30)
    - 12h00 FR : Clics depuis 00h00
    - 20h00 FR : Insta SOIR (vérif post 16h30)
    - Dimanche 20h05 : Récap hebdo
    - 1er du mois 09h35 : Récap mensuel

Alertes intelligentes :
    - Shadowban : Reel < 30% moy 7 précédents, 2 Reels consécutifs
    - Chute clics : Jour J < 50% du jour J-1
    - VA sous-perf : 3+ ratés (❌ + ⚠️) sur 7 jours (dans récap dimanche)

Commandes interactives (à taper dans le canal) :
    /today          — snapshot live de tous les comptes
    /stats USER     — stats d'un compte spécifique
    /week           — récap des 7 derniers jours
    /top            — top 3 Reels du jour
    /leaderboard    — classement VA live
    /pause USER     — met un compte en pause
    /resume USER    — réactive un compte en pause
    /help           — affiche les commandes

Variables d'environnement requises (Railway) :
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, RAPIDAPI_KEY, GMS_API_KEY,
    SUPABASE_URL, SUPABASE_KEY,
    GITHUB_TOKEN (PAT classic avec scope 'repo'),
    GITHUB_REPO  (format owner/repo, ex: lucasdessoly2-lgtm/bot-tracking-va)

Variables optionnelles :
    RAPIDAPI_HOST, GMS_HOST, GITHUB_BRANCH (par défaut 'main')
"""

import base64
import logging
import os
import re
import time as time_module
from datetime import datetime, time, timedelta, date
from typing import Optional

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler

from accounts import ACCOUNTS

# ============================================================================
#  CONFIGURATION
# ============================================================================

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
RAPIDAPI_KEY = os.environ["RAPIDAPI_KEY"]
GMS_API_KEY = os.environ.get("GMS_API_KEY")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")  # format: owner/repo
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
ACCOUNTS_FILE_PATH = "accounts.py"

RAPIDAPI_HOST = os.environ.get("RAPIDAPI_HOST", "instagram-scraper-20251.p.rapidapi.com")
GMS_HOST = os.environ.get("GMS_HOST", "api.getmysocial.com")
GMS_BASE_URL = f"https://{GMS_HOST}"

PARIS_TZ = pytz.timezone("Europe/Paris")

MATIN_TARGET = time(7, 30)
SOIR_TARGET = time(16, 30)
WINDOW_MINUTES = 30

# --- Seuils d'alertes ---
SHADOWBAN_DROP_RATIO = 0.30
SHADOWBAN_CONSECUTIVE = 2
SHADOWBAN_REFERENCE_REELS = 7
CLICKS_DROP_RATIO = 0.50
CLICKS_DROP_MIN_BASELINE = 10
VA_UNDERPERF_THRESHOLD = 3
VA_UNDERPERF_DAYS = 7
ALERT_DEDUP_HOURS = 24

# --- Cache GMS ---
_GMS_LINKS_CACHE: dict = {}
_GMS_CACHE_LAST_REFRESH: Optional[datetime] = None
_GMS_CACHE_TTL_HOURS = 6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bot")


# ============================================================================
#  TELEGRAM (send + polling)
# ============================================================================

def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if not r.ok:
            log.error("Telegram error %s: %s", r.status_code, r.text)
    except Exception as e:
        log.error("Telegram exception: %s", e)


def poll_telegram_updates(offset: int) -> list:
    """Long polling Telegram pour récupérer les messages entrants."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {
        "offset": offset,
        "timeout": 25,
        "allowed_updates": ["message", "channel_post"],
    }
    try:
        r = requests.get(url, params=params, timeout=40)
        if r.ok:
            return r.json().get("result", [])
        log.warning("getUpdates %s: %s", r.status_code, r.text[:200])
    except requests.exceptions.Timeout:
        pass  # normal pour long polling
    except Exception as e:
        log.error("Polling error: %s", e)
    return []


# ============================================================================
#  SUPABASE
# ============================================================================

def _sb_headers(prefer: str = "return=minimal") -> dict:
    return {
        "apikey": SUPABASE_KEY or "",
        "Authorization": f"Bearer {SUPABASE_KEY}" if SUPABASE_KEY else "",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def supabase_select(table: str, query_params: Optional[dict] = None) -> list:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    try:
        r = requests.get(
            url,
            headers=_sb_headers(prefer="return=representation"),
            params=query_params or {},
            timeout=30,
        )
        if r.ok:
            return r.json()
        log.warning("SB SELECT %s -> %s %s", table, r.status_code, r.text[:200])
    except Exception as e:
        log.error("SB SELECT %s exception: %s", table, e)
    return []


def supabase_upsert(table: str, payload: dict, on_conflict: str) -> bool:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    try:
        r = requests.post(
            url,
            headers=_sb_headers(prefer="resolution=merge-duplicates,return=minimal"),
            params={"on_conflict": on_conflict},
            json=payload,
            timeout=30,
        )
        if r.ok:
            return True
        log.warning("SB UPSERT %s -> %s %s", table, r.status_code, r.text[:200])
    except Exception as e:
        log.error("SB UPSERT %s exception: %s", table, e)
    return False


def supabase_insert(table: str, payload: dict) -> bool:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    try:
        r = requests.post(url, headers=_sb_headers(), json=payload, timeout=30)
        if r.ok:
            return True
        log.warning("SB INSERT %s -> %s %s", table, r.status_code, r.text[:200])
    except Exception as e:
        log.error("SB INSERT %s exception: %s", table, e)
    return False


# ============================================================================
#  GITHUB (commit auto pour /pause et /resume)
# ============================================================================

def github_get_file(file_path: str) -> tuple:
    """Renvoie (content_str, sha) du fichier ou (None, None) si erreur."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    try:
        r = requests.get(url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=30)
        if not r.ok:
            log.warning("GitHub GET %s -> %s %s", file_path, r.status_code, r.text[:200])
            return None, None
        data = r.json()
        content_b64 = data.get("content", "")
        sha = data.get("sha")
        try:
            content = base64.b64decode(content_b64).decode("utf-8")
        except Exception:
            return None, None
        return content, sha
    except Exception as e:
        log.error("GitHub GET exception: %s", e)
        return None, None


def github_put_file(file_path: str, new_content: str, sha: str, message: str) -> bool:
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "message": message,
        "content": base64.b64encode(new_content.encode("utf-8")).decode("utf-8"),
        "sha": sha,
        "branch": GITHUB_BRANCH,
    }
    try:
        r = requests.put(url, headers=headers, json=payload, timeout=30)
        if r.ok:
            return True
        log.warning("GitHub PUT %s -> %s %s", file_path, r.status_code, r.text[:200])
    except Exception as e:
        log.error("GitHub PUT exception: %s", e)
    return False


def toggle_account_pause(username: str, pause: bool) -> tuple:
    """Met en pause (pause=True) ou réactive (pause=False) un username dans accounts.py.
    Renvoie (success: bool, response_message: str)."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False, "⚠️ GitHub non configuré (GITHUB_TOKEN / GITHUB_REPO manquant)."

    content, sha = github_get_file(ACCOUNTS_FILE_PATH)
    if not content:
        return False, "⚠️ Impossible de lire accounts.py depuis GitHub."

    lines = content.splitlines(keepends=True)
    new_lines = []
    found_line_idx = None

    for idx, line in enumerate(lines):
        # Regarde si la ligne contient `("username", ...)` avec ou sans # devant
        # Match insensible à la casse
        if re.search(rf'\(\s*"{re.escape(username)}"\s*,', line, re.IGNORECASE):
            stripped = line.lstrip()
            is_commented = stripped.startswith("#")
            if pause:
                if is_commented:
                    new_lines.append(line)
                    return False, f"ℹ️ <code>{username}</code> est déjà en pause."
                # Ajouter # devant (en préservant l'indentation)
                indent = line[:len(line) - len(stripped)]
                line = f"{indent}# {stripped}"
                found_line_idx = idx
            else:
                if not is_commented:
                    new_lines.append(line)
                    return False, f"ℹ️ <code>{username}</code> n'est pas en pause."
                # Retirer le # devant
                indent = line[:len(line) - len(stripped)]
                rest = stripped[1:].lstrip()  # retire # et espaces
                line = f"{indent}{rest}"
                found_line_idx = idx
        new_lines.append(line)

    if found_line_idx is None:
        valid = [u for u, _ in ACCOUNTS if u.lower() == username.lower()]
        if valid:
            return False, f"⚠️ <code>{username}</code> n'est pas dans accounts.py (peut-être déjà retiré ?)."
        return False, f"❌ Compte <code>{username}</code> introuvable dans accounts.py."

    new_content = "".join(new_lines)
    action = "Pause" if pause else "Resume"
    commit_msg = f"{action} {username} via Telegram command"
    success = github_put_file(ACCOUNTS_FILE_PATH, new_content, sha, commit_msg)
    if success:
        verb = "mis en pause" if pause else "réactivé"
        return True, (
            f"✅ <code>{username}</code> {verb}.\n"
            f"Railway va redéployer automatiquement dans ~30 sec."
        )
    return False, "⚠️ Erreur lors du commit GitHub. Vérifie le GITHUB_TOKEN."


# ============================================================================
#  INSTAGRAM (via RapidAPI)
# ============================================================================

def fetch_recent_reels(username: str) -> list:
    url = f"https://{RAPIDAPI_HOST}/userreels"
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
    }
    try:
        r = requests.get(
            url,
            params={"username_or_id": username},
            headers=headers,
            timeout=30,
        )
        if not r.ok:
            log.warning("RapidAPI %s -> %s %s", username, r.status_code, r.text[:200])
            return []
        data = r.json()
        items = (
            data.get("data", {}).get("items")
            or data.get("items")
            or data.get("reels")
            or []
        )
        return items
    except Exception as e:
        log.error("Fetch Insta %s error: %s", username, e)
        return []


def parse_reel_stats(reel: dict) -> tuple:
    taken_at = (
        reel.get("taken_at")
        or reel.get("date")
        or reel.get("created_time")
        or reel.get("timestamp")
    )
    views = (
        reel.get("play_count")
        or reel.get("video_view_count")
        or reel.get("views")
        or reel.get("view_count")
        or 0
    )
    likes = reel.get("like_count") or reel.get("likes") or 0
    comments = reel.get("comment_count") or reel.get("comments") or 0
    return taken_at, views, likes, comments


def get_reel_shortcode(reel: dict) -> str:
    return str(reel.get("code") or reel.get("shortcode") or reel.get("pk") or "")


def reel_link(reel: dict) -> str:
    """Renvoie un lien HTML vers le Reel, ou chaîne vide si pas de shortcode."""
    sc = get_reel_shortcode(reel)
    if not sc:
        return ""
    return f' · <a href="https://www.instagram.com/reel/{sc}/">voir</a>'


def format_number(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def reel_post_dt(reel: dict) -> Optional[datetime]:
    taken_at, _, _, _ = parse_reel_stats(reel)
    if not taken_at:
        return None
    try:
        ts = int(taken_at)
        return datetime.fromtimestamp(ts, tz=pytz.UTC).astimezone(PARIS_TZ)
    except (ValueError, TypeError):
        return None


def find_post_in_window(items: list, target_time_paris: time):
    today_paris = datetime.now(PARIS_TZ).date()
    target_dt = PARIS_TZ.localize(datetime.combine(today_paris, target_time_paris))
    out_of_window = None
    for item in items:
        post_dt = reel_post_dt(item)
        if not post_dt or post_dt.date() != today_paris:
            continue
        delta_min = abs((post_dt - target_dt).total_seconds()) / 60
        if delta_min <= WINDOW_MINUTES:
            return "in_window", post_dt, item
        if out_of_window is None:
            out_of_window = (post_dt, item)
    if out_of_window:
        return "out_of_window", out_of_window[0], out_of_window[1]
    return "no_post", None, None


# ============================================================================
#  GETMYSOCIAL
# ============================================================================

def username_to_gms_shortcode(username: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", username.lower())


def gms_request(path: str, params: Optional[dict] = None) -> Optional[dict]:
    if not GMS_API_KEY:
        return None
    url = f"{GMS_BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {GMS_API_KEY}",
        "Accept": "application/json",
    }
    try:
        r = requests.get(url, headers=headers, params=params or {}, timeout=30)
        if not r.ok:
            log.warning("GMS %s -> %s %s", path, r.status_code, r.text[:200])
            return None
        return r.json()
    except Exception as e:
        log.error("GMS %s exception: %s", path, e)
        return None


def load_gms_links_map() -> dict:
    global _GMS_LINKS_CACHE, _GMS_CACHE_LAST_REFRESH
    now = datetime.now(PARIS_TZ)
    if (
        _GMS_LINKS_CACHE
        and _GMS_CACHE_LAST_REFRESH
        and (now - _GMS_CACHE_LAST_REFRESH).total_seconds() < _GMS_CACHE_TTL_HOURS * 3600
    ):
        return _GMS_LINKS_CACHE
    mapping: dict = {}
    cursor = None
    page = 0
    while True:
        page += 1
        params = {"limit": 100, "sort": "-created"}
        if cursor:
            params["cursor"] = cursor
        data = gms_request("/v3/links", params=params)
        if not data:
            break
        for item in data.get("data", []):
            sc = (item.get("shortcode") or "").lower()
            link_id = item.get("id")
            if sc and link_id:
                mapping[sc] = link_id
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
        if page > 20:
            break
    _GMS_LINKS_CACHE = mapping
    _GMS_CACHE_LAST_REFRESH = now
    log.info("GMS links map refreshed: %d entries", len(mapping))
    return mapping


def find_gms_link_id(username: str, links_map: dict) -> Optional[str]:
    candidates = [
        username_to_gms_shortcode(username),
        username.lower(),
        username.lower().replace(".", "-"),
        username.lower().replace(".", "_"),
    ]
    for c in candidates:
        if c in links_map:
            return links_map[c]
    return None


def gms_date_range(period: str) -> tuple:
    now_paris = datetime.now(PARIS_TZ)
    if period == "yesterday":
        d = (now_paris - timedelta(days=1)).date()
        start = PARIS_TZ.localize(datetime.combine(d, time(0, 0)))
        end = PARIS_TZ.localize(datetime.combine(d, time(23, 59, 59)))
    else:
        d = now_paris.date()
        start = PARIS_TZ.localize(datetime.combine(d, time(0, 0)))
        end = now_paris
    return start.astimezone(pytz.UTC).isoformat(), end.astimezone(pytz.UTC).isoformat()


def fetch_gms_clicks_and_countries(link_id: str, period: str) -> tuple:
    start_iso, end_iso = gms_date_range(period)
    common_params_variants = [
        {"link_id": link_id, "start_date": start_iso, "end_date": end_iso},
        {"link_id": link_id, "from": start_iso, "to": end_iso},
        {"link_id": link_id, "start": start_iso, "end": end_iso},
    ]
    total_clicks: Optional[int] = None
    countries: list = []
    for params in common_params_variants:
        overview = gms_request("/v3/analytics/overview", params=params)
        if overview:
            total_clicks = (
                overview.get("clicks")
                or overview.get("total_clicks")
                or overview.get("visits")
                or (overview.get("data") or {}).get("clicks")
            )
            if total_clicks is not None:
                break
    for params in common_params_variants:
        breakdown = gms_request("/v3/analytics/breakdowns/country", params=params)
        if breakdown:
            rows = breakdown.get("data") or breakdown.get("rows") or []
            if rows:
                parsed = []
                for r in rows:
                    code = (
                        r.get("country_code")
                        or r.get("code")
                        or r.get("country")
                        or r.get("key")
                        or "??"
                    )
                    val = (
                        r.get("clicks")
                        or r.get("visits")
                        or r.get("count")
                        or r.get("value")
                        or 0
                    )
                    parsed.append((code, val))
                parsed.sort(key=lambda x: x[1], reverse=True)
                total = sum(v for _, v in parsed) or 1
                countries = [(c, round(v * 100 / total)) for c, v in parsed[:3]]
                break
    return total_clicks, countries


# ============================================================================
#  PERSISTENCE
# ============================================================================

def save_reel(username: str, reel: dict) -> None:
    taken_at, views, likes, comments = parse_reel_stats(reel)
    if not taken_at:
        return
    try:
        ts = int(taken_at)
        dt_iso = datetime.fromtimestamp(ts, tz=pytz.UTC).isoformat()
    except (ValueError, TypeError):
        return
    supabase_upsert(
        "reels_history",
        {
            "username": username,
            "reel_shortcode": get_reel_shortcode(reel),
            "taken_at": dt_iso,
            "views": int(views) if views else 0,
            "likes": int(likes) if likes else 0,
            "comments": int(comments) if comments else 0,
        },
        on_conflict="username,taken_at",
    )


def save_post_status(username: str, va_name: str, slot: str, status: str,
                     post_dt: Optional[datetime], item: Optional[dict]) -> None:
    today_paris = datetime.now(PARIS_TZ).date()
    payload = {
        "username": username,
        "va_name": va_name,
        "date": today_paris.isoformat(),
        "slot": slot,
        "status": status,
    }
    if post_dt:
        payload["post_time"] = post_dt.strftime("%H:%M:%S")
    if item:
        _, views, _, _ = parse_reel_stats(item)
        payload["reel_shortcode"] = get_reel_shortcode(item)
        payload["views"] = int(views) if views else 0
    supabase_upsert("post_status", payload, on_conflict="username,date,slot")


def save_clicks(username: str, period: str, clicks: Optional[int], top_countries: list) -> None:
    if period == "yesterday":
        target_date = (datetime.now(PARIS_TZ) - timedelta(days=1)).date()
    else:
        target_date = datetime.now(PARIS_TZ).date()
    supabase_upsert(
        "daily_clicks",
        {
            "username": username,
            "date": target_date.isoformat(),
            "clicks": int(clicks) if clicks else 0,
            "top_countries": [{"code": c, "pct": p} for c, p in (top_countries or [])],
        },
        on_conflict="username,date",
    )


# ============================================================================
#  ALERTES
# ============================================================================

def has_recent_alert(alert_type: str, target: str, hours: int = ALERT_DEDUP_HOURS) -> bool:
    since = (datetime.now(pytz.UTC) - timedelta(hours=hours)).isoformat()
    rows = supabase_select(
        "alerts_log",
        {
            "alert_type": f"eq.{alert_type}",
            "target": f"eq.{target}",
            "triggered_at": f"gte.{since}",
            "order": "triggered_at.desc",
            "limit": 1,
        },
    )
    return len(rows) > 0


def log_and_send_alert(alert_type: str, target: str, message: str,
                       details: Optional[dict] = None) -> None:
    if has_recent_alert(alert_type, target):
        log.info("Alert %s/%s skipped (recent dup)", alert_type, target)
        return
    send_telegram(message)
    supabase_insert("alerts_log", {
        "alert_type": alert_type,
        "target": target,
        "details": details or {},
    })


def detect_shadowban(username: str) -> None:
    rows = supabase_select(
        "reels_history",
        {
            "username": f"eq.{username}",
            "order": "taken_at.desc",
            "limit": SHADOWBAN_REFERENCE_REELS + SHADOWBAN_CONSECUTIVE,
        },
    )
    if len(rows) < SHADOWBAN_REFERENCE_REELS + SHADOWBAN_CONSECUTIVE:
        return
    last_n = rows[:SHADOWBAN_CONSECUTIVE]
    reference = rows[SHADOWBAN_CONSECUTIVE:SHADOWBAN_CONSECUTIVE + SHADOWBAN_REFERENCE_REELS]
    avg_views = sum(r.get("views", 0) for r in reference) / max(len(reference), 1)
    if avg_views < 100:
        return
    threshold = avg_views * SHADOWBAN_DROP_RATIO
    for r in last_n:
        if (r.get("views", 0) or 0) >= threshold:
            return
    last_views = last_n[0].get("views", 0)
    msg = (
        f"🔇 <b>ALERTE SHADOWBAN</b>\n"
        f"<code>{username}</code> en chute libre\n"
        f"Dernier Reel : <b>{format_number(last_views)} vues</b>\n"
        f"Moyenne {SHADOWBAN_REFERENCE_REELS} précédents : <b>{format_number(int(avg_views))}</b>\n"
        f"{SHADOWBAN_CONSECUTIVE} Reels consécutifs à -70%+. À vérifier."
    )
    log_and_send_alert("shadowban", username, msg, {
        "avg_views": int(avg_views),
        "last_views": int(last_views),
    })


def detect_clicks_drop(username: str, today_clicks: int) -> None:
    yesterday = (datetime.now(PARIS_TZ) - timedelta(days=1)).date()
    rows = supabase_select(
        "daily_clicks",
        {
            "username": f"eq.{username}",
            "date": f"eq.{yesterday.isoformat()}",
            "limit": 1,
        },
    )
    if not rows:
        return
    yest_clicks = rows[0].get("clicks", 0) or 0
    if yest_clicks < CLICKS_DROP_MIN_BASELINE:
        return
    if today_clicks >= yest_clicks * CLICKS_DROP_RATIO:
        return
    pct = int((today_clicks - yest_clicks) / yest_clicks * 100)
    msg = (
        f"📉 <b>ALERTE CHUTE CLICS</b>\n"
        f"<code>{username}</code>\n"
        f"Aujourd'hui : <b>{today_clicks}</b> clics\n"
        f"Hier : <b>{yest_clicks}</b> clics\n"
        f"Évolution : <b>{pct}%</b>"
    )
    log_and_send_alert("clicks_drop", username, msg, {
        "today": today_clicks,
        "yesterday": yest_clicks,
    })


def detect_va_underperf_for_recap() -> list:
    today = datetime.now(PARIS_TZ).date()
    since = (today - timedelta(days=VA_UNDERPERF_DAYS - 1)).isoformat()
    rows = supabase_select(
        "post_status",
        {
            "date": f"gte.{since}",
            "order": "date.desc",
        },
    )
    if not rows:
        return []
    misses_by_va: dict = {}
    total_by_va: dict = {}
    for r in rows:
        va = r.get("va_name", "?")
        total_by_va[va] = total_by_va.get(va, 0) + 1
        if r.get("status") in ("no_post", "out_of_window"):
            misses_by_va[va] = misses_by_va.get(va, 0) + 1
    result = []
    for va, miss_count in misses_by_va.items():
        if miss_count >= VA_UNDERPERF_THRESHOLD:
            result.append((va, miss_count, total_by_va.get(va, 0)))
    result.sort(key=lambda x: x[1], reverse=True)
    return result


# ============================================================================
#  RAPPORTS INSTAGRAM (matin/soir)
# ============================================================================

def generate_insta_report(target_time_paris: time, label: str, slot_name: str) -> str:
    now_paris = datetime.now(PARIS_TZ)

    va_groups: dict = {}
    for username, va_name in ACCOUNTS:
        va_groups.setdefault(va_name, []).append(username)

    lines = []
    date_str = now_paris.strftime("%A %d %B %Y %H:%M")
    lines.append(f"📊 <b>RAPPORT {label}</b> — {date_str}")
    lines.append("")

    total_ok = total_out = total_missing = total_accounts = 0

    for va_name, usernames in va_groups.items():
        va_ok = va_out = va_missing = 0
        va_lines = []
        for username in usernames:
            total_accounts += 1
            reels = fetch_recent_reels(username)
            for r in reels[:10]:
                save_reel(username, r)
            status, post_dt, item = find_post_in_window(reels, target_time_paris)
            save_post_status(username, va_name, slot_name, status, post_dt, item)
            detect_shadowban(username)

            if status == "in_window":
                va_ok += 1
                total_ok += 1
                _, views, likes, comments = parse_reel_stats(item)
                hhmm = post_dt.strftime("%Hh%M")
                va_lines.append(
                    f"  ✅ <code>{username}</code> — Posté {hhmm}{reel_link(item)}\n"
                    f"     👁 {format_number(views)} vues · "
                    f"❤️ {format_number(likes)} · "
                    f"💬 {format_number(comments)}"
                )
            elif status == "out_of_window":
                va_out += 1
                total_out += 1
                _, views, likes, comments = parse_reel_stats(item)
                hhmm = post_dt.strftime("%Hh%M")
                va_lines.append(
                    f"  ⚠️ <code>{username}</code> — Hors créneau ({hhmm}){reel_link(item)}\n"
                    f"     👁 {format_number(views)} vues · "
                    f"❤️ {format_number(likes)} · "
                    f"💬 {format_number(comments)}"
                )
            else:
                va_missing += 1
                total_missing += 1
                va_lines.append(
                    f"  ❌ <code>{username}</code> — Pas de post {label.lower()}"
                )

        lines.append(
            f"👤 <b>{va_name}</b> ({len(usernames)} comptes) "
            f"→ {va_ok}✅ / {va_out}⚠️ / {va_missing}❌"
        )
        lines.extend(va_lines)
        lines.append("")

    lines.append(
        f"📈 <b>TOTAL : {total_ok}✅ / {total_out}⚠️ / {total_missing}❌</b> "
        f"sur {total_accounts} comptes"
    )

    return "\n".join(lines)


# ============================================================================
#  RAPPORTS CLICS (00h/12h)
# ============================================================================

def generate_clicks_report(period: str, label: str, header_emoji: str) -> str:
    now_paris = datetime.now(PARIS_TZ)

    va_groups: dict = {}
    for username, va_name in ACCOUNTS:
        va_groups.setdefault(va_name, []).append(username)

    lines = []
    date_str = now_paris.strftime("%A %d %B %Y %H:%M")
    lines.append(f"{header_emoji} <b>RAPPORT {label}</b> — {date_str}")
    lines.append("")

    if not GMS_API_KEY:
        lines.append("⚠️ <i>GMS_API_KEY non configurée — clics indisponibles</i>")
        return "\n".join(lines)

    links_map = load_gms_links_map()
    if not links_map:
        lines.append("⚠️ <i>Impossible de récupérer la liste des liens GMS</i>")
        return "\n".join(lines)

    total_clicks_global = 0

    for va_name, usernames in va_groups.items():
        va_lines = []
        for username in usernames:
            link_id = find_gms_link_id(username, links_map)
            if not link_id:
                va_lines.append(f"  ❓ <code>{username}</code> — Lien GMS introuvable")
                continue
            clicks, countries = fetch_gms_clicks_and_countries(link_id, period)
            if clicks is None:
                va_lines.append(f"  ⚠️ <code>{username}</code> — Stats indisponibles")
                continue
            save_clicks(username, period, clicks, countries)
            if period == "yesterday":
                detect_clicks_drop(username, int(clicks))
            total_clicks_global += clicks
            countries_str = (
                " ".join(f"{c} ({p}%)" for c, p in countries) if countries else "—"
            )
            va_lines.append(
                f"  🔗 <code>{username}</code> — {format_number(clicks)} clics\n"
                f"     🌍 {countries_str}"
            )

        lines.append(f"👤 <b>{va_name}</b> ({len(usernames)} comptes)")
        lines.extend(va_lines)
        lines.append("")

    lines.append(f"📈 <b>TOTAL : {format_number(total_clicks_global)} clics</b>")
    return "\n".join(lines)


# ============================================================================
#  AGRÉGATIONS (récap hebdo / mensuel + leaderboard)
# ============================================================================

def fetch_aggregated_clicks(start_date: date, end_date: date) -> dict:
    rows = supabase_select(
        "daily_clicks",
        {"date": f"gte.{start_date.isoformat()}", "limit": 10000},
    )
    result: dict = {}
    for r in rows:
        d = r.get("date", "")
        try:
            d_obj = datetime.fromisoformat(d).date()
        except Exception:
            continue
        if d_obj > end_date:
            continue
        u = r.get("username")
        c = r.get("clicks", 0) or 0
        result[u] = result.get(u, 0) + c
    return result


def fetch_aggregated_views(start_date: date, end_date: date) -> dict:
    rows = supabase_select(
        "reels_history",
        {"taken_at": f"gte.{start_date.isoformat()}", "limit": 10000},
    )
    result: dict = {}
    for r in rows:
        ta = r.get("taken_at", "")
        try:
            ta_dt = datetime.fromisoformat(ta.replace("Z", "+00:00"))
        except Exception:
            continue
        if ta_dt.date() > end_date:
            continue
        u = r.get("username")
        v = r.get("views", 0) or 0
        result[u] = result.get(u, 0) + v
    return result


def aggregate_country_clicks(start_date: date, end_date: date) -> list:
    rows = supabase_select(
        "daily_clicks",
        {"date": f"gte.{start_date.isoformat()}", "limit": 10000},
    )
    country_totals: dict = {}
    for r in rows:
        d = r.get("date", "")
        try:
            d_obj = datetime.fromisoformat(d).date()
        except Exception:
            continue
        if d_obj > end_date:
            continue
        clicks = r.get("clicks", 0) or 0
        countries = r.get("top_countries") or []
        for entry in countries:
            code = entry.get("code") or "??"
            pct = entry.get("pct", 0) or 0
            country_totals[code] = country_totals.get(code, 0) + clicks * pct / 100
    if not country_totals:
        return []
    total = sum(country_totals.values()) or 1
    items = sorted(country_totals.items(), key=lambda x: x[1], reverse=True)[:3]
    return [(c, round(v * 100 / total)) for c, v in items]


def generate_recap_hebdo() -> str:
    now_paris = datetime.now(PARIS_TZ)
    today = now_paris.date()
    week_start = today - timedelta(days=6)
    prev_week_start = today - timedelta(days=13)
    prev_week_end = today - timedelta(days=7)

    lines = [
        f"📅 <b>RÉCAP HEBDO</b> — semaine du {week_start.strftime('%d/%m')} au {today.strftime('%d/%m')}",
        "",
    ]

    va_to_users: dict = {}
    for username, va_name in ACCOUNTS:
        va_to_users.setdefault(va_name, []).append(username)

    clicks_this_week = fetch_aggregated_clicks(week_start, today)
    clicks_prev_week = fetch_aggregated_clicks(prev_week_start, prev_week_end)
    views_this_week = fetch_aggregated_views(week_start, today)
    views_prev_week = fetch_aggregated_views(prev_week_start, prev_week_end)

    va_scores: list = []
    for va_name, usernames in va_to_users.items():
        total_clicks = sum(clicks_this_week.get(u, 0) for u in usernames)
        nb = len(usernames) or 1
        avg = total_clicks / nb
        va_scores.append((va_name, total_clicks, avg, nb))
    va_scores.sort(key=lambda x: x[2], reverse=True)

    lines.append("🏆 <b>Classement VA (clics moyens / compte)</b>")
    medals = ["🥇", "🥈", "🥉"]
    for i, (va, total, avg, nb) in enumerate(va_scores):
        medal = medals[i] if i < 3 else "•"
        lines.append(
            f"  {medal} <b>{va}</b> — {format_number(int(avg))} clics moy. "
            f"({format_number(int(total))} total · {nb} comptes)"
        )
    lines.append("")

    total_clicks_now = sum(clicks_this_week.values())
    total_clicks_prev = sum(clicks_prev_week.values())
    if total_clicks_prev > 0:
        evo_pct = (total_clicks_now - total_clicks_prev) / total_clicks_prev * 100
        arrow = "📈" if evo_pct >= 0 else "📉"
        sign = "+" if evo_pct >= 0 else ""
        lines.append(
            f"🔗 <b>Clics totaux :</b> {format_number(total_clicks_now)} "
            f"({arrow} {sign}{evo_pct:.0f}% vs sem. dernière {format_number(total_clicks_prev)})"
        )
    else:
        lines.append(f"🔗 <b>Clics totaux :</b> {format_number(total_clicks_now)} "
                     f"(pas de comparaison)")

    total_views_now = sum(views_this_week.values())
    total_views_prev = sum(views_prev_week.values())
    if total_views_prev > 0:
        evo_pct = (total_views_now - total_views_prev) / total_views_prev * 100
        arrow = "📈" if evo_pct >= 0 else "📉"
        sign = "+" if evo_pct >= 0 else ""
        lines.append(
            f"👁 <b>Vues totales :</b> {format_number(total_views_now)} "
            f"({arrow} {sign}{evo_pct:.0f}% vs sem. dernière {format_number(total_views_prev)})"
        )
    else:
        lines.append(f"👁 <b>Vues totales :</b> {format_number(total_views_now)} "
                     f"(pas de comparaison)")

    top_countries = aggregate_country_clicks(week_start, today)
    if top_countries:
        cstr = " · ".join(f"{c} ({p}%)" for c, p in top_countries)
        lines.append(f"🌍 <b>Top 3 pays :</b> {cstr}")
    lines.append("")

    underperf = detect_va_underperf_for_recap()
    if underperf:
        lines.append("⚠️ <b>VA en sous-perf cette semaine</b>")
        for va, miss, total in underperf:
            lines.append(f"  • <b>{va}</b> — {miss} créneaux ratés sur {total}")
        lines.append("")
    else:
        lines.append("✅ <i>Aucun VA en sous-perf cette semaine</i>")
        lines.append("")

    return "\n".join(lines)


def generate_recap_mensuel() -> str:
    now_paris = datetime.now(PARIS_TZ)
    today = now_paris.date()
    last_day_prev = today.replace(day=1) - timedelta(days=1)
    first_day_prev = last_day_prev.replace(day=1)
    days_in_month = (last_day_prev - first_day_prev).days + 1
    last_day_prev_prev = first_day_prev - timedelta(days=1)
    first_day_prev_prev = last_day_prev_prev.replace(day=1)

    month_name = first_day_prev.strftime("%B %Y")

    lines = [f"📆 <b>RÉCAP MENSUEL — {month_name}</b>", ""]

    clicks_m = fetch_aggregated_clicks(first_day_prev, last_day_prev)
    clicks_m_prev = fetch_aggregated_clicks(first_day_prev_prev, last_day_prev_prev)
    views_m = fetch_aggregated_views(first_day_prev, last_day_prev)
    views_m_prev = fetch_aggregated_views(first_day_prev_prev, last_day_prev_prev)

    va_to_users: dict = {}
    for username, va_name in ACCOUNTS:
        va_to_users.setdefault(va_name, []).append(username)

    va_scores: list = []
    for va_name, usernames in va_to_users.items():
        total = sum(clicks_m.get(u, 0) for u in usernames)
        nb = len(usernames) or 1
        avg = total / nb
        va_scores.append((va_name, total, avg, nb))
    va_scores.sort(key=lambda x: x[2], reverse=True)

    lines.append(f"🏆 <b>Classement VA ({days_in_month} jours)</b>")
    medals = ["🥇", "🥈", "🥉"]
    for i, (va, total, avg, nb) in enumerate(va_scores):
        medal = medals[i] if i < 3 else "•"
        lines.append(
            f"  {medal} <b>{va}</b> — {format_number(int(avg))} clics moy. "
            f"({format_number(int(total))} total · {nb} comptes)"
        )
    lines.append("")

    total_clicks = sum(clicks_m.values())
    total_clicks_prev = sum(clicks_m_prev.values())
    if total_clicks_prev > 0:
        evo = (total_clicks - total_clicks_prev) / total_clicks_prev * 100
        arrow = "📈" if evo >= 0 else "📉"
        sign = "+" if evo >= 0 else ""
        lines.append(
            f"🔗 <b>Clics totaux :</b> {format_number(total_clicks)} "
            f"({arrow} {sign}{evo:.0f}% vs mois précédent)"
        )
    else:
        lines.append(f"🔗 <b>Clics totaux :</b> {format_number(total_clicks)}")

    total_views = sum(views_m.values())
    total_views_prev = sum(views_m_prev.values())
    if total_views_prev > 0:
        evo = (total_views - total_views_prev) / total_views_prev * 100
        arrow = "📈" if evo >= 0 else "📉"
        sign = "+" if evo >= 0 else ""
        lines.append(
            f"👁 <b>Vues totales :</b> {format_number(total_views)} "
            f"({arrow} {sign}{evo:.0f}% vs mois précédent)"
        )
    else:
        lines.append(f"👁 <b>Vues totales :</b> {format_number(total_views)}")

    top_countries = aggregate_country_clicks(first_day_prev, last_day_prev)
    if top_countries:
        cstr = " · ".join(f"{c} ({p}%)" for c, p in top_countries)
        lines.append(f"🌍 <b>Top 3 pays :</b> {cstr}")

    return "\n".join(lines)


# ============================================================================
#  COMMANDES INTERACTIVES (handlers)
# ============================================================================

def cmd_help() -> str:
    return (
        "🤖 <b>Commandes disponibles</b>\n\n"
        "<b>Consultation</b>\n"
        "  /today — snapshot live de tous les comptes\n"
        "  /stats &lt;username&gt; — stats d'un compte\n"
        "  /week — récap des 7 derniers jours\n"
        "  /top — top 3 Reels du jour\n"
        "  /leaderboard — classement VA live\n\n"
        "<b>Action</b>\n"
        "  /pause &lt;username&gt; — met un compte en pause\n"
        "  /resume &lt;username&gt; — réactive un compte\n"
        "  /help — affiche cette aide"
    )


def cmd_today() -> str:
    now_paris = datetime.now(PARIS_TZ)
    today = now_paris.date()

    va_groups: dict = {}
    for username, va_name in ACCOUNTS:
        va_groups.setdefault(va_name, []).append(username)

    lines = [
        f"📸 <b>SNAPSHOT LIVE</b> — {now_paris.strftime('%d/%m %H:%M')}",
        "",
    ]
    total_posts = 0

    for va_name, usernames in va_groups.items():
        lines.append(f"👤 <b>{va_name}</b>")
        for username in usernames:
            reels = fetch_recent_reels(username)
            today_reels = []
            for r in reels[:8]:
                post_dt = reel_post_dt(r)
                if post_dt and post_dt.date() == today:
                    today_reels.append((post_dt, r))
            if today_reels:
                total_posts += len(today_reels)
                lines.append(f"  ✅ <code>{username}</code> ({len(today_reels)} post(s) aujourd'hui)")
                for post_dt, r in today_reels:
                    _, views, likes, _ = parse_reel_stats(r)
                    lines.append(
                        f"     {post_dt.strftime('%Hh%M')} — "
                        f"👁 {format_number(views)} · ❤️ {format_number(likes)}{reel_link(r)}"
                    )
            else:
                lines.append(f"  ❌ <code>{username}</code> — pas de post aujourd'hui")
        lines.append("")

    lines.append(f"📈 <b>{total_posts} posts aujourd'hui</b>")
    return "\n".join(lines)


def cmd_stats(username: str) -> str:
    valid_usernames = [u for u, _ in ACCOUNTS]
    matched = None
    for u in valid_usernames:
        if u.lower() == username.lower():
            matched = u
            break
    if not matched:
        return (
            f"❌ Compte <code>{username}</code> non trouvé.\n\n"
            f"Comptes valides :\n" +
            "\n".join(f"  • <code>{u}</code>" for u in valid_usernames)
        )
    username = matched

    now_paris = datetime.now(PARIS_TZ)
    today = now_paris.date()
    yesterday = (now_paris - timedelta(days=1)).date()

    reels = fetch_recent_reels(username)

    lines = [
        f"📊 <b>STATS LIVE — {username}</b>",
        f"<i>{now_paris.strftime('%d/%m %H:%M')}</i>",
        "",
    ]

    if not reels:
        lines.append("⚠️ Impossible de récupérer les Reels.")
        return "\n".join(lines)

    lines.append("<b>5 derniers Reels</b>")
    for r in reels[:5]:
        _, views, likes, comments = parse_reel_stats(r)
        post_dt = reel_post_dt(r)
        date_str = post_dt.strftime("%d/%m %Hh%M") if post_dt else "?"
        lines.append(
            f"  • {date_str} — 👁 {format_number(views)} · "
            f"❤️ {format_number(likes)} · 💬 {format_number(comments)}{reel_link(r)}"
        )
    lines.append("")

    lines.append("<b>Clics GMS</b>")
    rows_today = supabase_select(
        "daily_clicks",
        {"username": f"eq.{username}", "date": f"eq.{today.isoformat()}", "limit": 1},
    )
    rows_yest = supabase_select(
        "daily_clicks",
        {"username": f"eq.{username}", "date": f"eq.{yesterday.isoformat()}", "limit": 1},
    )

    if rows_today:
        clicks = rows_today[0].get("clicks", 0) or 0
        countries = rows_today[0].get("top_countries", []) or []
        cstr = " · ".join(f"{c.get('code', '??')} ({c.get('pct', 0)}%)" for c in countries[:3]) if countries else "—"
        lines.append(f"  Aujourd'hui : <b>{format_number(clicks)}</b> clics · 🌍 {cstr}")
    else:
        lines.append("  Aujourd'hui : pas encore de data")

    if rows_yest:
        lines.append(f"  Hier : <b>{format_number(rows_yest[0].get('clicks', 0) or 0)}</b> clics")

    return "\n".join(lines)


def cmd_top() -> str:
    now_paris = datetime.now(PARIS_TZ)
    today = now_paris.date()

    all_reels = []
    for username, va_name in ACCOUNTS:
        reels = fetch_recent_reels(username)
        for r in reels[:5]:
            post_dt = reel_post_dt(r)
            if not post_dt or post_dt.date() != today:
                continue
            _, views, likes, comments = parse_reel_stats(r)
            all_reels.append((username, va_name, post_dt, views, likes, comments, r))

    if not all_reels:
        return "📭 Aucun Reel publié aujourd'hui."

    all_reels.sort(key=lambda x: x[3] or 0, reverse=True)
    top3 = all_reels[:3]

    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"🔥 <b>TOP 3 REELS DU JOUR</b>",
        f"<i>{now_paris.strftime('%d/%m %H:%M')}</i>",
        "",
    ]
    for i, (username, va_name, post_dt, views, likes, comments, r) in enumerate(top3):
        lines.append(
            f"{medals[i]} <code>{username}</code> ({va_name})\n"
            f"   Posté {post_dt.strftime('%Hh%M')}{reel_link(r)}\n"
            f"   👁 {format_number(views)} · ❤️ {format_number(likes)} · 💬 {format_number(comments)}"
        )

    return "\n".join(lines)


def cmd_leaderboard() -> str:
    today = datetime.now(PARIS_TZ).date()
    week_start = today - timedelta(days=6)

    clicks = fetch_aggregated_clicks(week_start, today)

    va_to_users: dict = {}
    for username, va_name in ACCOUNTS:
        va_to_users.setdefault(va_name, []).append(username)

    va_scores = []
    for va_name, usernames in va_to_users.items():
        total = sum(clicks.get(u, 0) for u in usernames)
        nb = len(usernames) or 1
        avg = total / nb
        va_scores.append((va_name, total, avg, nb))
    va_scores.sort(key=lambda x: x[2], reverse=True)

    lines = [f"🏆 <b>CLASSEMENT VA</b> (7 derniers jours)", ""]
    medals = ["🥇", "🥈", "🥉"]
    for i, (va, total, avg, nb) in enumerate(va_scores):
        medal = medals[i] if i < 3 else "•"
        lines.append(
            f"{medal} <b>{va}</b> — {format_number(int(avg))} clics moy. "
            f"({format_number(int(total))} total · {nb} comptes)"
        )
    return "\n".join(lines)


def handle_command(text: str) -> Optional[str]:
    """Dispatch d'une commande. Renvoie la réponse ou None si commande inconnue."""
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    # Gère /command@bot_name
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]

    try:
        if cmd == "/help" or cmd == "/start":
            return cmd_help()
        elif cmd == "/today":
            return cmd_today()
        elif cmd == "/stats":
            if not args:
                return "Usage : <code>/stats &lt;username&gt;</code>"
            return cmd_stats(args[0])
        elif cmd == "/week":
            return generate_recap_hebdo()
        elif cmd == "/top":
            return cmd_top()
        elif cmd == "/leaderboard":
            return cmd_leaderboard()
        elif cmd == "/pause":
            if not args:
                return "Usage : <code>/pause &lt;username&gt;</code>"
            _, msg = toggle_account_pause(args[0], pause=True)
            return msg
        elif cmd == "/resume":
            if not args:
                return "Usage : <code>/resume &lt;username&gt;</code>"
            _, msg = toggle_account_pause(args[0], pause=False)
            return msg
    except Exception as e:
        log.error("Command %s exception: %s", cmd, e)
        return f"⚠️ Erreur lors de l'exécution de {cmd}"
    return None


def telegram_polling_loop() -> None:
    """Boucle infinie de polling Telegram. Tourne dans le thread principal."""
    log.info("Telegram polling started")
    # On commence avec un offset basé sur le dernier update connu (skip backlog)
    initial = poll_telegram_updates(-1)
    last_update_id = initial[-1]["update_id"] + 1 if initial else 0

    while True:
        try:
            updates = poll_telegram_updates(last_update_id)
            for update in updates:
                last_update_id = max(last_update_id, update["update_id"] + 1)
                message = update.get("message") or update.get("channel_post")
                if not message:
                    continue
                text = message.get("text", "")
                if not text or not text.startswith("/"):
                    continue
                chat_id = message.get("chat", {}).get("id")
                # Filtre : on n'accepte les commandes que depuis le canal configuré
                if str(chat_id) != str(TELEGRAM_CHAT_ID):
                    continue
                response = handle_command(text)
                if response:
                    send_telegram(response)
        except Exception as e:
            log.error("Polling loop error: %s", e)
            time_module.sleep(5)


# ============================================================================
#  JOBS PROGRAMMÉS
# ============================================================================

def job_insta_matin() -> None:
    log.info("Job INSTA MATIN")
    send_telegram(generate_insta_report(MATIN_TARGET, "INSTA MATIN", "matin"))


def job_insta_soir() -> None:
    log.info("Job INSTA SOIR")
    send_telegram(generate_insta_report(SOIR_TARGET, "INSTA SOIR", "soir"))


def job_clics_minuit() -> None:
    log.info("Job CLICS MINUIT (jour J-1)")
    send_telegram(generate_clicks_report("yesterday", "CLICS — JOUR COMPLET", "🌙"))


def job_clics_midi() -> None:
    log.info("Job CLICS MIDI (depuis 00h)")
    send_telegram(generate_clicks_report("today", "CLICS — MI-JOURNÉE", "☀️"))


def job_recap_hebdo() -> None:
    log.info("Job RECAP HEBDO")
    send_telegram(generate_recap_hebdo())


def job_recap_mensuel() -> None:
    log.info("Job RECAP MENSUEL")
    send_telegram(generate_recap_mensuel())


# ============================================================================
#  STARTUP / MAIN
# ============================================================================

def send_startup_message() -> None:
    nb_comptes = len(ACCOUNTS)
    nb_va = len({va for _, va in ACCOUNTS})
    gms_status = "✅ activé" if GMS_API_KEY else "⚠️ désactivé"
    sb_status = "✅ activé" if (SUPABASE_URL and SUPABASE_KEY) else "⚠️ désactivé"
    gh_status = "✅ activé" if (GITHUB_TOKEN and GITHUB_REPO) else "⚠️ désactivé"
    msg = (
        "🟢 <b>Bot démarré</b>\n"
        f"📊 {nb_comptes} comptes surveillés\n"
        f"👥 {nb_va} VA\n"
        f"🔗 GetMySocial : {gms_status}\n"
        f"💾 Supabase : {sb_status}\n"
        f"🛠 GitHub auto-commit : {gh_status}\n"
        "⏰ Rapports automatiques :\n"
        "   🌙 00h00 — Clics jour complet\n"
        "   🌅 09h30 — Insta matin\n"
        "   ☀️ 12h00 — Clics mi-journée\n"
        "   🌆 20h00 — Insta soir\n"
        "   📅 Dimanche 20h05 — Récap hebdo\n"
        "   📆 1er du mois 09h35 — Récap mensuel\n"
        "💬 Tape <code>/help</code> pour voir les commandes"
    )
    send_telegram(msg)


def main() -> None:
    log.info("Starting bot — %d comptes surveillés", len(ACCOUNTS))
    send_startup_message()

    scheduler = BackgroundScheduler(timezone=PARIS_TZ)
    scheduler.add_job(job_clics_minuit, "cron", hour=0,  minute=0)
    scheduler.add_job(job_insta_matin,  "cron", hour=9,  minute=30)
    scheduler.add_job(job_clics_midi,   "cron", hour=12, minute=0)
    scheduler.add_job(job_insta_soir,   "cron", hour=20, minute=0)
    scheduler.add_job(job_recap_hebdo,  "cron", day_of_week="sun", hour=20, minute=5)
    scheduler.add_job(job_recap_mensuel,"cron", day=1, hour=9, minute=35)
    scheduler.start()

    log.info("Scheduler started in background — entering Telegram polling loop")
    telegram_polling_loop()


if __name__ == "__main__":
    main()
