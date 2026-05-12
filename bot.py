"""
Bot Telegram — Tracking VA Instagram + GetMySocial + Supabase (v2.1)
---------------------------------------------------------------------
Refonte sans créneaux horaires précis.
Objectif simple : 4 posts par compte par jour.

Rapports automatiques :
    - 00h00 FR : Clics jour J-1 complet
    - 12h00 FR : Clics depuis 00h00
    - 20h00 FR : Bilan Insta du jour (X/4 posts par compte + total vues + best)
    - Dimanche 20h05 : Récap hebdo
    - 1er du mois 09h35 : Récap mensuel

Alertes intelligentes :
    - Shadowban : Reel < 30% moy 7 précédents, 2 Reels consécutifs
    - Chute clics : Jour J < 50% du jour J-1
    - VA sous-perf : un compte du VA <4 posts pendant 3+ jours sur 7 (récap dimanche)

Commandes interactives (à taper dans le canal) :
    /today, /stats <user>, /week, /top, /leaderboard,
    /pause <user>, /resume <user>, /help

Variables d'environnement requises (Railway) :
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, RAPIDAPI_KEY, GMS_API_KEY,
    SUPABASE_URL, SUPABASE_KEY, GITHUB_TOKEN, GITHUB_REPO

Format accounts.py :
    Tuples à 2 éléments  : ("username_insta", "NOM_VA")
    Tuples à 3 éléments  : ("username_insta", "NOM_VA", "shortcode_gms")
    Le 3ème champ est optionnel.
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
GITHUB_REPO = os.environ.get("GITHUB_REPO")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
ACCOUNTS_FILE_PATH = "accounts.py"

RAPIDAPI_HOST = os.environ.get("RAPIDAPI_HOST", "instagram-scraper-20251.p.rapidapi.com")
GMS_HOST = os.environ.get("GMS_HOST", "api.getmysocial.com")
GMS_BASE_URL = f"https://{GMS_HOST}"

PARIS_TZ = pytz.timezone("Europe/Paris")

# Objectif posts/jour par compte
DAILY_POSTS_TARGET = 4

# Seuils d'alertes
SHADOWBAN_DROP_RATIO = 0.30
SHADOWBAN_CONSECUTIVE = 2
SHADOWBAN_REFERENCE_REELS = 7
CLICKS_DROP_RATIO = 0.50
CLICKS_DROP_MIN_BASELINE = 10
VA_UNDERPERF_DAYS_MISSING = 3   # nb jours où un compte rate l'objectif
VA_UNDERPERF_LOOKBACK = 7       # sur les 7 derniers jours
ALERT_DEDUP_HOURS = 24

# Cache GMS
_GMS_LINKS_CACHE: dict = {}
_GMS_CACHE_LAST_REFRESH: Optional[datetime] = None
_GMS_CACHE_TTL_HOURS = 6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bot")


# ============================================================================
#  HELPERS — ACCOUNTS (gère tuples à 2 ou 3 éléments)
# ============================================================================

def account_username(account) -> str:
    return account[0]


def account_va(account) -> str:
    return account[1]


def account_gms_override(account) -> Optional[str]:
    """Renvoie le shortcode GMS forcé (3ème champ) ou None."""
    if len(account) >= 3:
        sc = account[2]
        if sc and not sc.startswith("TODO"):
            return sc.lower()
    return None


def iter_accounts():
    """Itère sur les comptes en yieldant (username, va, gms_override)."""
    for a in ACCOUNTS:
        yield account_username(a), account_va(a), account_gms_override(a)


def group_by_va() -> dict:
    """Renvoie { va_name: [(username, gms_override), ...] }."""
    groups: dict = {}
    for u, va, gms in iter_accounts():
        groups.setdefault(va, []).append((u, gms))
    return groups


def all_usernames() -> list:
    return [u for u, _, _ in iter_accounts()]


# ============================================================================
#  TELEGRAM
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
        pass
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
            log.warning("GH GET %s -> %s", file_path, r.status_code)
            return None, None
        data = r.json()
        try:
            content = base64.b64decode(data.get("content", "")).decode("utf-8")
        except Exception:
            return None, None
        return content, data.get("sha")
    except Exception as e:
        log.error("GH GET exception: %s", e)
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
        log.warning("GH PUT %s -> %s %s", file_path, r.status_code, r.text[:200])
    except Exception as e:
        log.error("GH PUT exception: %s", e)
    return False


def toggle_account_pause(username: str, pause: bool) -> tuple:
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False, "⚠️ GitHub non configuré."
    content, sha = github_get_file(ACCOUNTS_FILE_PATH)
    if not content:
        return False, "⚠️ Impossible de lire accounts.py depuis GitHub."
    lines = content.splitlines(keepends=True)
    new_lines = []
    found_line_idx = None
    for idx, line in enumerate(lines):
        if re.search(rf'\(\s*"{re.escape(username)}"\s*,', line, re.IGNORECASE):
            stripped = line.lstrip()
            is_commented = stripped.startswith("#")
            if pause:
                if is_commented:
                    new_lines.append(line)
                    return False, f"ℹ️ <code>{username}</code> est déjà en pause."
                indent = line[:len(line) - len(stripped)]
                line = f"{indent}# {stripped}"
                found_line_idx = idx
            else:
                if not is_commented:
                    new_lines.append(line)
                    return False, f"ℹ️ <code>{username}</code> n'est pas en pause."
                indent = line[:len(line) - len(stripped)]
                rest = stripped[1:].lstrip()
                line = f"{indent}{rest}"
                found_line_idx = idx
        new_lines.append(line)
    if found_line_idx is None:
        return False, f"❌ Compte <code>{username}</code> introuvable dans accounts.py."
    new_content = "".join(new_lines)
    action = "Pause" if pause else "Resume"
    success = github_put_file(ACCOUNTS_FILE_PATH, new_content, sha,
                              f"{action} {username} via Telegram")
    if success:
        verb = "mis en pause" if pause else "réactivé"
        return True, (
            f"✅ <code>{username}</code> {verb}.\n"
            f"Railway va redéployer dans ~30 sec."
        )
    return False, "⚠️ Erreur lors du commit GitHub."


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
            log.warning("RapidAPI %s -> %s", username, r.status_code)
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


def reel_url(reel: dict) -> Optional[str]:
    sc = get_reel_shortcode(reel)
    if not sc:
        return None
    return f"https://www.instagram.com/reel/{sc}/"


def reel_link_html(reel: dict, label: str = "voir") -> str:
    u = reel_url(reel)
    if not u:
        return ""
    return f'<a href="{u}">{label}</a>'


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


def collect_today_posts(reels: list) -> list:
    """Filtre les Reels publiés aujourd'hui (heure de Paris).
    Renvoie une liste triée chronologiquement de tuples (post_dt, reel)."""
    today_paris = datetime.now(PARIS_TZ).date()
    posts = []
    for r in reels:
        dt = reel_post_dt(r)
        if dt and dt.date() == today_paris:
            posts.append((dt, r))
    posts.sort(key=lambda x: x[0])
    return posts


def status_emoji(count: int) -> str:
    """Emoji selon le nombre de posts du jour vs DAILY_POSTS_TARGET."""
    if count >= DAILY_POSTS_TARGET:
        return "🟢"
    if count == 0:
        return "🔴"
    return "🟡"


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
            log.warning("GMS %s -> %s", path, r.status_code)
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
    log.info("GMS links map: %d entries", len(mapping))
    return mapping


def find_gms_link_id(username: str, gms_override: Optional[str], links_map: dict) -> Optional[str]:
    """Trouve le link_id GMS pour un username Insta.
    Si gms_override fourni, l'utilise en priorité.
    """
    if gms_override and gms_override in links_map:
        return links_map[gms_override]
    candidates = [
        username_to_gms_shortcode(username),
        username.lower(),
        username.lower().replace(".", "-"),
        username.lower().replace(".", "_"),
        username.lower().replace(".", ""),
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
    variants = [
        {"link_id": link_id, "start_date": start_iso, "end_date": end_iso},
        {"link_id": link_id, "from": start_iso, "to": end_iso},
        {"link_id": link_id, "start": start_iso, "end": end_iso},
    ]
    total_clicks: Optional[int] = None
    countries: list = []
    for params in variants:
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
    for params in variants:
        breakdown = gms_request("/v3/analytics/breakdowns/country", params=params)
        if breakdown:
            rows = breakdown.get("data") or breakdown.get("rows") or []
            if rows:
                parsed = []
                for r in rows:
                    code = r.get("country_code") or r.get("code") or r.get("country") or r.get("key") or "??"
                    val = r.get("clicks") or r.get("visits") or r.get("count") or r.get("value") or 0
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


def save_daily_posts(username: str, va_name: str, posts: list) -> tuple:
    """posts = liste de tuples (post_dt, reel). Renvoie (count, total_views, best_views, best_url)."""
    count = len(posts)
    total_views = 0
    best_views = 0
    best_url = None
    posts_json = []
    for post_dt, reel in posts:
        _, views, likes, comments = parse_reel_stats(reel)
        v = int(views) if views else 0
        total_views += v
        if v > best_views:
            best_views = v
            best_url = reel_url(reel)
        posts_json.append({
            "time": post_dt.strftime("%H:%M"),
            "shortcode": get_reel_shortcode(reel),
            "views": v,
            "likes": int(likes) if likes else 0,
            "comments": int(comments) if comments else 0,
        })
    today_paris = datetime.now(PARIS_TZ).date()
    supabase_upsert(
        "daily_posts",
        {
            "username": username,
            "va_name": va_name,
            "date": today_paris.isoformat(),
            "post_count": count,
            "total_views": total_views,
            "best_views": best_views,
            "best_url": best_url or "",
            "posts": posts_json,
        },
        on_conflict="username,date",
    )
    return count, total_views, best_views, best_url


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
        f"Aujourd'hui : <b>{today_clicks}</b> · Hier : <b>{yest_clicks}</b> "
        f"({pct}%)"
    )
    log_and_send_alert("clicks_drop", username, msg)


def detect_va_underperf_for_recap() -> list:
    """Renvoie liste (va_name, [(username, jours_ratés), ...]) pour les VA avec ≥1 compte raté ≥3 jours."""
    today = datetime.now(PARIS_TZ).date()
    since = (today - timedelta(days=VA_UNDERPERF_LOOKBACK - 1)).isoformat()
    rows = supabase_select(
        "daily_posts",
        {"date": f"gte.{since}", "limit": 10000},
    )
    if not rows:
        return []
    misses_by_user: dict = {}
    user_to_va: dict = {}
    for r in rows:
        u = r.get("username")
        va = r.get("va_name", "?")
        user_to_va[u] = va
        cnt = r.get("post_count", 0) or 0
        if cnt < DAILY_POSTS_TARGET:
            misses_by_user[u] = misses_by_user.get(u, 0) + 1
    flagged_by_va: dict = {}
    for u, miss_count in misses_by_user.items():
        if miss_count >= VA_UNDERPERF_DAYS_MISSING:
            va = user_to_va.get(u, "?")
            flagged_by_va.setdefault(va, []).append((u, miss_count))
    result = sorted(flagged_by_va.items(), key=lambda x: -sum(m for _, m in x[1]))
    return result


# ============================================================================
#  RAPPORT BILAN INSTA (20h00)
# ============================================================================

def generate_insta_recap() -> str:
    """Rapport unique en fin de journée : X/4 posts par compte + vues + best."""
    now_paris = datetime.now(PARIS_TZ)
    date_str = now_paris.strftime("%A %d %B %Y %H:%M")

    va_groups = group_by_va()

    lines = [f"📊 <b>BILAN INSTA</b> — {date_str}", ""]

    grand_total_posts = 0
    grand_target_posts = 0
    grand_total_views = 0

    for va_name, accounts in va_groups.items():
        va_lines = []
        va_posts = 0
        va_target = len(accounts) * DAILY_POSTS_TARGET
        va_views = 0
        for username, _ in accounts:
            reels = fetch_recent_reels(username)
            for r in reels[:10]:
                save_reel(username, r)
            today_posts = collect_today_posts(reels)
            count, total_views, best_views, best_url = save_daily_posts(
                username, va_name, today_posts
            )
            detect_shadowban(username)

            va_posts += count
            va_views += total_views
            emoji = status_emoji(count)

            base = (
                f"  {emoji} <code>{username}</code> — "
                f"<b>{count}/{DAILY_POSTS_TARGET}</b> posts · "
                f"👁 {format_number(total_views)} vues"
            )
            if best_views > 0:
                if best_url:
                    base += f' · <a href="{best_url}">best {format_number(best_views)}</a>'
                else:
                    base += f' · best {format_number(best_views)}'
            va_lines.append(base)

        grand_total_posts += va_posts
        grand_target_posts += va_target
        grand_total_views += va_views

        lines.append(
            f"👤 <b>{va_name}</b> — {va_posts}/{va_target} posts · "
            f"👁 {format_number(va_views)} vues"
        )
        lines.extend(va_lines)
        lines.append("")

    lines.append(
        f"📈 <b>TOTAL : {grand_total_posts}/{grand_target_posts} posts</b> · "
        f"👁 <b>{format_number(grand_total_views)} vues</b>"
    )
    return "\n".join(lines)


# ============================================================================
#  RAPPORT CLICS (00h et 12h)
# ============================================================================

def generate_clicks_report(period: str, label: str, header_emoji: str) -> str:
    now_paris = datetime.now(PARIS_TZ)
    va_groups = group_by_va()

    lines = [
        f"{header_emoji} <b>RAPPORT {label}</b> — "
        f"{now_paris.strftime('%A %d %B %Y %H:%M')}",
        "",
    ]

    if not GMS_API_KEY:
        lines.append("⚠️ <i>GMS_API_KEY non configurée</i>")
        return "\n".join(lines)

    links_map = load_gms_links_map()
    if not links_map:
        lines.append("⚠️ <i>Impossible de récupérer les liens GMS</i>")
        return "\n".join(lines)

    total_clicks_global = 0

    for va_name, accounts in va_groups.items():
        va_lines = []
        for username, gms_override in accounts:
            link_id = find_gms_link_id(username, gms_override, links_map)
            if not link_id:
                hint = f" (cherché : {gms_override})" if gms_override else ""
                va_lines.append(
                    f"  ❓ <code>{username}</code> — Lien GMS introuvable{hint}"
                )
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
                f"  🔗 <code>{username}</code> — {format_number(clicks)} clics · 🌍 {countries_str}"
            )

        lines.append(f"👤 <b>{va_name}</b>")
        lines.extend(va_lines)
        lines.append("")

    lines.append(f"📈 <b>TOTAL : {format_number(total_clicks_global)} clics</b>")
    return "\n".join(lines)


# ============================================================================
#  AGRÉGATIONS (récap hebdo / mensuel)
# ============================================================================

def fetch_aggregated_clicks(start_date: date, end_date: date) -> dict:
    rows = supabase_select(
        "daily_clicks",
        {"date": f"gte.{start_date.isoformat()}", "limit": 10000},
    )
    result: dict = {}
    for r in rows:
        try:
            d_obj = datetime.fromisoformat(r.get("date", "")).date()
        except Exception:
            continue
        if d_obj > end_date:
            continue
        u = r.get("username")
        result[u] = result.get(u, 0) + (r.get("clicks", 0) or 0)
    return result


def fetch_aggregated_daily_posts(start_date: date, end_date: date) -> dict:
    """Renvoie { username: {"posts": int, "views": int} }."""
    rows = supabase_select(
        "daily_posts",
        {"date": f"gte.{start_date.isoformat()}", "limit": 10000},
    )
    result: dict = {}
    for r in rows:
        try:
            d_obj = datetime.fromisoformat(r.get("date", "")).date()
        except Exception:
            continue
        if d_obj > end_date:
            continue
        u = r.get("username")
        if u not in result:
            result[u] = {"posts": 0, "views": 0}
        result[u]["posts"] += r.get("post_count", 0) or 0
        result[u]["views"] += r.get("total_views", 0) or 0
    return result


def aggregate_country_clicks(start_date: date, end_date: date) -> list:
    rows = supabase_select(
        "daily_clicks",
        {"date": f"gte.{start_date.isoformat()}", "limit": 10000},
    )
    country_totals: dict = {}
    for r in rows:
        try:
            d_obj = datetime.fromisoformat(r.get("date", "")).date()
        except Exception:
            continue
        if d_obj > end_date:
            continue
        clicks = r.get("clicks", 0) or 0
        for entry in (r.get("top_countries") or []):
            code = entry.get("code") or "??"
            pct = entry.get("pct", 0) or 0
            country_totals[code] = country_totals.get(code, 0) + clicks * pct / 100
    if not country_totals:
        return []
    total = sum(country_totals.values()) or 1
    items = sorted(country_totals.items(), key=lambda x: x[1], reverse=True)[:3]
    return [(c, round(v * 100 / total)) for c, v in items]


def generate_recap_hebdo() -> str:
    today = datetime.now(PARIS_TZ).date()
    week_start = today - timedelta(days=6)
    prev_week_start = today - timedelta(days=13)
    prev_week_end = today - timedelta(days=7)

    lines = [
        f"📅 <b>RÉCAP HEBDO</b> — semaine du {week_start.strftime('%d/%m')} au {today.strftime('%d/%m')}",
        "",
    ]

    va_groups = group_by_va()

    clicks_now = fetch_aggregated_clicks(week_start, today)
    clicks_prev = fetch_aggregated_clicks(prev_week_start, prev_week_end)
    posts_now = fetch_aggregated_daily_posts(week_start, today)
    posts_prev = fetch_aggregated_daily_posts(prev_week_start, prev_week_end)

    # Classement VA (clics moyens / compte)
    va_scores = []
    for va_name, accounts in va_groups.items():
        usernames = [u for u, _ in accounts]
        total = sum(clicks_now.get(u, 0) for u in usernames)
        nb = len(usernames) or 1
        va_scores.append((va_name, total, total / nb, nb))
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

    # Évolution clics
    tc_now = sum(clicks_now.values())
    tc_prev = sum(clicks_prev.values())
    if tc_prev > 0:
        evo = (tc_now - tc_prev) / tc_prev * 100
        arrow = "📈" if evo >= 0 else "📉"
        sign = "+" if evo >= 0 else ""
        lines.append(
            f"🔗 <b>Clics :</b> {format_number(tc_now)} ({arrow} {sign}{evo:.0f}% vs sem. dernière)"
        )
    else:
        lines.append(f"🔗 <b>Clics :</b> {format_number(tc_now)} (pas de comparaison)")

    # Évolution vues
    tv_now = sum(d["views"] for d in posts_now.values())
    tv_prev = sum(d["views"] for d in posts_prev.values())
    if tv_prev > 0:
        evo = (tv_now - tv_prev) / tv_prev * 100
        arrow = "📈" if evo >= 0 else "📉"
        sign = "+" if evo >= 0 else ""
        lines.append(
            f"👁 <b>Vues :</b> {format_number(tv_now)} ({arrow} {sign}{evo:.0f}% vs sem. dernière)"
        )
    else:
        lines.append(f"👁 <b>Vues :</b> {format_number(tv_now)} (pas de comparaison)")

    # Posts totaux
    tp_now = sum(d["posts"] for d in posts_now.values())
    nb_accounts = len(all_usernames())
    target_week = nb_accounts * DAILY_POSTS_TARGET * 7
    lines.append(f"📤 <b>Posts :</b> {tp_now}/{target_week} ({tp_now*100//max(target_week,1)}% de l'objectif)")

    # Top pays
    top_countries = aggregate_country_clicks(week_start, today)
    if top_countries:
        cstr = " · ".join(f"{c} ({p}%)" for c, p in top_countries)
        lines.append(f"🌍 <b>Top 3 pays :</b> {cstr}")
    lines.append("")

    # VA sous-perf
    underperf = detect_va_underperf_for_recap()
    if underperf:
        lines.append("⚠️ <b>Comptes en sous-perf</b> (3+ jours sous l'objectif)")
        for va, accounts_flagged in underperf:
            lines.append(f"  <b>{va}</b>")
            for u, miss_days in accounts_flagged:
                lines.append(f"    • <code>{u}</code> — {miss_days} jours sous 4 posts")
        lines.append("")
    else:
        lines.append("✅ <i>Aucun compte en sous-perf cette semaine</i>")

    return "\n".join(lines)


def generate_recap_mensuel() -> str:
    today = datetime.now(PARIS_TZ).date()
    last_day_prev = today.replace(day=1) - timedelta(days=1)
    first_day_prev = last_day_prev.replace(day=1)
    days_in_month = (last_day_prev - first_day_prev).days + 1
    last_day_prev_prev = first_day_prev - timedelta(days=1)
    first_day_prev_prev = last_day_prev_prev.replace(day=1)
    month_name = first_day_prev.strftime("%B %Y")

    lines = [f"📆 <b>RÉCAP MENSUEL — {month_name}</b>", ""]

    va_groups = group_by_va()
    clicks_m = fetch_aggregated_clicks(first_day_prev, last_day_prev)
    clicks_m_prev = fetch_aggregated_clicks(first_day_prev_prev, last_day_prev_prev)
    posts_m = fetch_aggregated_daily_posts(first_day_prev, last_day_prev)
    posts_m_prev = fetch_aggregated_daily_posts(first_day_prev_prev, last_day_prev_prev)

    va_scores = []
    for va_name, accounts in va_groups.items():
        usernames = [u for u, _ in accounts]
        total = sum(clicks_m.get(u, 0) for u in usernames)
        nb = len(usernames) or 1
        va_scores.append((va_name, total, total / nb, nb))
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

    tc = sum(clicks_m.values())
    tc_p = sum(clicks_m_prev.values())
    if tc_p > 0:
        evo = (tc - tc_p) / tc_p * 100
        arrow = "📈" if evo >= 0 else "📉"
        sign = "+" if evo >= 0 else ""
        lines.append(f"🔗 <b>Clics :</b> {format_number(tc)} ({arrow} {sign}{evo:.0f}% vs mois précédent)")
    else:
        lines.append(f"🔗 <b>Clics :</b> {format_number(tc)}")

    tv = sum(d["views"] for d in posts_m.values())
    tv_p = sum(d["views"] for d in posts_m_prev.values())
    if tv_p > 0:
        evo = (tv - tv_p) / tv_p * 100
        arrow = "📈" if evo >= 0 else "📉"
        sign = "+" if evo >= 0 else ""
        lines.append(f"👁 <b>Vues :</b> {format_number(tv)} ({arrow} {sign}{evo:.0f}% vs mois précédent)")
    else:
        lines.append(f"👁 <b>Vues :</b> {format_number(tv)}")

    tp = sum(d["posts"] for d in posts_m.values())
    nb_accounts = len(all_usernames())
    target = nb_accounts * DAILY_POSTS_TARGET * days_in_month
    lines.append(f"📤 <b>Posts :</b> {tp}/{target} ({tp*100//max(target,1)}% de l'objectif)")

    top_countries = aggregate_country_clicks(first_day_prev, last_day_prev)
    if top_countries:
        cstr = " · ".join(f"{c} ({p}%)" for c, p in top_countries)
        lines.append(f"🌍 <b>Top 3 pays :</b> {cstr}")

    return "\n".join(lines)


# ============================================================================
#  COMMANDES INTERACTIVES
# ============================================================================

def cmd_help() -> str:
    return (
        "🤖 <b>Commandes disponibles</b>\n\n"
        "<b>Consultation</b>\n"
        "  /today — bilan live de tous les comptes (X/4 posts)\n"
        "  /stats &lt;username&gt; — stats détaillées d'un compte\n"
        "  /week — récap des 7 derniers jours\n"
        "  /top — top 3 Reels du jour\n"
        "  /leaderboard — classement VA live\n\n"
        "<b>Action</b>\n"
        "  /pause &lt;username&gt; — met un compte en pause\n"
        "  /resume &lt;username&gt; — réactive un compte\n"
        "  /help — affiche cette aide\n\n"
        f"<i>Objectif quotidien : {DAILY_POSTS_TARGET} posts par compte</i>"
    )


def cmd_today() -> str:
    """Bilan live (sans attendre 20h)."""
    return generate_insta_recap().replace("BILAN INSTA", "SNAPSHOT LIVE")


def cmd_stats(username: str) -> str:
    valid = all_usernames()
    matched = next((u for u in valid if u.lower() == username.lower()), None)
    if not matched:
        return (
            f"❌ Compte <code>{username}</code> non trouvé.\n\n"
            "Comptes valides :\n" +
            "\n".join(f"  • <code>{u}</code>" for u in valid)
        )
    username = matched
    now_paris = datetime.now(PARIS_TZ)
    today = now_paris.date()
    yesterday = (now_paris - timedelta(days=1)).date()

    reels = fetch_recent_reels(username)
    if not reels:
        return f"⚠️ Impossible de récupérer les Reels de <code>{username}</code>."

    today_posts = collect_today_posts(reels)
    count = len(today_posts)
    total_views = sum(int(parse_reel_stats(r)[1] or 0) for _, r in today_posts)

    emoji = status_emoji(count)
    lines = [
        f"📊 <b>STATS — {username}</b>",
        f"<i>{now_paris.strftime('%d/%m %H:%M')}</i>",
        "",
        f"{emoji} <b>Aujourd'hui : {count}/{DAILY_POSTS_TARGET} posts · 👁 {format_number(total_views)} vues</b>",
        "",
    ]

    if today_posts:
        lines.append("<b>Posts du jour</b>")
        for post_dt, r in today_posts:
            _, views, likes, _ = parse_reel_stats(r)
            lines.append(
                f"  • {post_dt.strftime('%H:%M')} — "
                f"👁 {format_number(views)} · ❤️ {format_number(likes)} · {reel_link_html(r)}"
            )
        lines.append("")

    lines.append("<b>5 derniers Reels (tous)</b>")
    for r in reels[:5]:
        _, views, likes, _ = parse_reel_stats(r)
        post_dt = reel_post_dt(r)
        date_str = post_dt.strftime("%d/%m %H:%M") if post_dt else "?"
        lines.append(
            f"  • {date_str} — 👁 {format_number(views)} · ❤️ {format_number(likes)} · {reel_link_html(r)}"
        )
    lines.append("")

    # Clics
    rows_today = supabase_select(
        "daily_clicks",
        {"username": f"eq.{username}", "date": f"eq.{today.isoformat()}", "limit": 1},
    )
    rows_yest = supabase_select(
        "daily_clicks",
        {"username": f"eq.{username}", "date": f"eq.{yesterday.isoformat()}", "limit": 1},
    )
    lines.append("<b>Clics GMS</b>")
    if rows_today:
        c = rows_today[0].get("clicks", 0) or 0
        countries = rows_today[0].get("top_countries", []) or []
        cstr = " · ".join(f"{x.get('code','??')} ({x.get('pct',0)}%)" for x in countries[:3]) if countries else "—"
        lines.append(f"  Aujourd'hui : <b>{format_number(c)}</b> · 🌍 {cstr}")
    else:
        lines.append("  Aujourd'hui : pas encore de data")
    if rows_yest:
        lines.append(f"  Hier : <b>{format_number(rows_yest[0].get('clicks',0) or 0)}</b>")

    return "\n".join(lines)


def cmd_top() -> str:
    now_paris = datetime.now(PARIS_TZ)
    today = now_paris.date()
    all_reels = []
    for username, va_name, _ in iter_accounts():
        reels = fetch_recent_reels(username)
        for r in reels[:5]:
            dt = reel_post_dt(r)
            if dt and dt.date() == today:
                _, views, likes, comments = parse_reel_stats(r)
                all_reels.append((username, va_name, dt, views, likes, comments, r))
    if not all_reels:
        return "📭 Aucun Reel publié aujourd'hui."
    all_reels.sort(key=lambda x: x[3] or 0, reverse=True)
    top3 = all_reels[:3]
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🔥 <b>TOP 3 REELS DU JOUR</b>", f"<i>{now_paris.strftime('%d/%m %H:%M')}</i>", ""]
    for i, (u, va, dt, v, lk, cm, r) in enumerate(top3):
        lines.append(
            f"{medals[i]} <code>{u}</code> ({va}) — {dt.strftime('%H:%M')}\n"
            f"   👁 {format_number(v)} · ❤️ {format_number(lk)} · 💬 {format_number(cm)} · {reel_link_html(r)}"
        )
    return "\n".join(lines)


def cmd_leaderboard() -> str:
    today = datetime.now(PARIS_TZ).date()
    week_start = today - timedelta(days=6)
    clicks = fetch_aggregated_clicks(week_start, today)
    va_groups = group_by_va()
    va_scores = []
    for va_name, accounts in va_groups.items():
        usernames = [u for u, _ in accounts]
        total = sum(clicks.get(u, 0) for u in usernames)
        nb = len(usernames) or 1
        va_scores.append((va_name, total, total / nb, nb))
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
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]
    try:
        if cmd in ("/help", "/start"):
            return cmd_help()
        if cmd == "/today":
            return cmd_today()
        if cmd == "/stats":
            if not args:
                return "Usage : <code>/stats &lt;username&gt;</code>"
            return cmd_stats(args[0])
        if cmd == "/week":
            return generate_recap_hebdo()
        if cmd == "/top":
            return cmd_top()
        if cmd == "/leaderboard":
            return cmd_leaderboard()
        if cmd == "/pause":
            if not args:
                return "Usage : <code>/pause &lt;username&gt;</code>"
            _, msg = toggle_account_pause(args[0], pause=True)
            return msg
        if cmd == "/resume":
            if not args:
                return "Usage : <code>/resume &lt;username&gt;</code>"
            _, msg = toggle_account_pause(args[0], pause=False)
            return msg
    except Exception as e:
        log.error("Command %s exception: %s", cmd, e)
        return f"⚠️ Erreur lors de l'exécution de {cmd}"
    return None


def telegram_polling_loop() -> None:
    log.info("Telegram polling started")
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

def job_insta_recap() -> None:
    log.info("Job INSTA RECAP 20h")
    send_telegram(generate_insta_recap())


def job_clics_minuit() -> None:
    log.info("Job CLICS MINUIT")
    send_telegram(generate_clicks_report("yesterday", "CLICS — JOUR COMPLET", "🌙"))


def job_clics_midi() -> None:
    log.info("Job CLICS MIDI")
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
    nb_comptes = len(all_usernames())
    nb_va = len(group_by_va())
    gms_status = "✅ activé" if GMS_API_KEY else "⚠️ désactivé"
    sb_status = "✅ activé" if (SUPABASE_URL and SUPABASE_KEY) else "⚠️ désactivé"
    gh_status = "✅ activé" if (GITHUB_TOKEN and GITHUB_REPO) else "⚠️ désactivé"
    # Compter les overrides GMS configurés
    nb_overrides = sum(1 for _, _, g in iter_accounts() if g)
    msg = (
        "🟢 <b>Bot démarré</b> (v2.1)\n"
        f"📊 {nb_comptes} comptes surveillés (objectif : {DAILY_POSTS_TARGET} posts/jour)\n"
        f"👥 {nb_va} VA\n"
        f"🔗 GetMySocial : {gms_status}\n"
        f"💾 Supabase : {sb_status}\n"
        f"🛠 GitHub auto-commit : {gh_status}\n"
        f"🎯 Mappings GMS manuels : {nb_overrides}/{nb_comptes}\n"
        "⏰ Rapports automatiques :\n"
        "   🌙 00h00 — Clics jour complet\n"
        "   ☀️ 12h00 — Clics mi-journée\n"
        "   🌆 20h00 — Bilan Insta\n"
        "   📅 Dimanche 20h05 — Récap hebdo\n"
        "   📆 1er du mois 09h35 — Récap mensuel\n"
        "💬 Tape <code>/help</code> pour voir les commandes"
    )
    send_telegram(msg)


def main() -> None:
    log.info("Starting bot — %d comptes surveillés", len(all_usernames()))
    send_startup_message()

    scheduler = BackgroundScheduler(timezone=PARIS_TZ)
    scheduler.add_job(job_clics_minuit, "cron", hour=0,  minute=0)
    scheduler.add_job(job_clics_midi,   "cron", hour=12, minute=0)
    scheduler.add_job(job_insta_recap,  "cron", hour=20, minute=0)
    scheduler.add_job(job_recap_hebdo,  "cron", day_of_week="sun", hour=20, minute=5)
    scheduler.add_job(job_recap_mensuel,"cron", day=1, hour=9, minute=35)
    scheduler.start()

    log.info("Scheduler started in background — entering Telegram polling loop")
    telegram_polling_loop()


if __name__ == "__main__":
    main()
