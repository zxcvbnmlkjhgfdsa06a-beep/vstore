from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from riot_auth import RiotAuthError



# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------
APP_ROOT = Path(__file__).resolve().parent
import os
from pathlib import Path

if os.environ.get("VERCEL"):
    DATA_DIR = Path("/tmp/data")
else:
    DATA_DIR = Path(__file__).resolve().parent / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "vstore.db"

if os.environ.get("VERCEL"):
    UPLOAD_DIR = Path("/tmp/uploads")
else:
    UPLOAD_DIR = APP_ROOT / "static" / "uploads"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
else:
    UPLOAD_DIR = APP_ROOT / "static" / "uploads"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
LOGO_PATH = str(APP_ROOT / "assets" / "logo.png")
BANNER_ART_PATH = str(APP_ROOT / "assets" / "banner.png")

IS_PROD = os.environ.get("VSTORE_ENV", "").lower() in ("prod", "production", "1")

_secret = os.environ.get("VSTORE_SECRET")
if IS_PROD and not _secret:
    raise RuntimeError(
        "VSTORE_SECRET chưa được set — bắt buộc khi VSTORE_ENV=production. "
        "Tạo bằng: python -c \"import secrets;print(secrets.token_hex(32))\""
    )

app = Flask(__name__)
app.config.update(
    TEMPLATES_AUTO_RELOAD=not IS_PROD,
    SECRET_KEY=_secret or "dev-only-not-secret-please-set-VSTORE_SECRET",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PROD,          # HTTPS-only cookies in prod
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,     # 1 MB cap on request bodies
)

if IS_PROD:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Limits
USERNAME_RE         = re.compile(r"^[A-Za-z0-9_\-]{3,24}$")
RIOT_USERNAME_RE    = re.compile(r"^[A-Za-z0-9_\.\-]{2,32}$")
PASSWORD_MIN        = 8
SCANS_PER_DAY_LIMIT = 20


# ---------------------------------------------------------------------------
# Region → Riot shard routing
# ---------------------------------------------------------------------------
# Riot only has 4 actual shards (na / eu / ap / kr) but we expose a finer
# region selector for UX, and translate to shard server-side.
REGION_TO_SHARD: dict[str, str] = {
    "ap": "ap",
    "kr": "kr",
    "eu": "eu",
    "na": "na",
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT    UNIQUE NOT NULL,
  email         TEXT,
  password_hash TEXT    NOT NULL,
  is_admin      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
  last_login_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS scans (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id             TEXT    UNIQUE NOT NULL,
  riot_username      TEXT,
  display_name       TEXT,
  puuid              TEXT,
  region             TEXT,
  shard              TEXT,
  skins_found        INTEGER,
  skins_total        INTEGER,
  image_url          TEXT,
  access_token       TEXT,
  entitlements_token TEXT,
  skin_list_json     TEXT,
  account_info_json  TEXT,
  mask_riot_id       INTEGER DEFAULT 0,
  include_battlepass INTEGER DEFAULT 0,
  store_json         TEXT,
  created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_scans_user_created ON scans(user_id, created_at DESC);
"""

# Idempotent migrations for existing DBs.
_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN is_admin           INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE scans ADD COLUMN display_name       TEXT",
    "ALTER TABLE scans ADD COLUMN puuid              TEXT",
    "ALTER TABLE scans ADD COLUMN access_token       TEXT",
    "ALTER TABLE scans ADD COLUMN entitlements_token TEXT",
    "ALTER TABLE scans ADD COLUMN skin_list_json     TEXT",
    "ALTER TABLE scans ADD COLUMN account_info_json  TEXT",
    "ALTER TABLE scans ADD COLUMN mask_riot_id       INTEGER DEFAULT 0",
    "ALTER TABLE scans ADD COLUMN include_battlepass INTEGER DEFAULT 0",
    "ALTER TABLE scans ADD COLUMN store_json         TEXT",
]


def get_db() -> sqlite3.Connection:
    db = getattr(g, "_db", None)
    if db is None:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")   # better read concurrency
        db.execute("PRAGMA synchronous = NORMAL")  # safe + faster than FULL
        db.execute("PRAGMA cache_size = -8000")    # 8 MB page cache per connection
        db.execute("PRAGMA temp_store = MEMORY")
        g._db = db
    return db


@app.teardown_appcontext
def close_db(_exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                # Column already exists — fine.
                pass
        conn.commit()


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = os.environ.get("VSTORE_ADMIN_PASSWORD") or ("manhquan99" if not IS_PROD else "")
if IS_PROD and not ADMIN_PASSWORD:
    raise RuntimeError(
        "VSTORE_ADMIN_PASSWORD chưa được set — bắt buộc khi VSTORE_ENV=production."
    )


def seed_admin() -> None:
    """Create (or promote) the admin account so exactly one admin exists."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)
        ).fetchone()
        if row:
            conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (row["id"],))
        else:
            conn.execute(
                "INSERT INTO users (username, email, password_hash, is_admin) "
                "VALUES (?, ?, ?, 1)",
                (ADMIN_USERNAME, None, generate_password_hash(ADMIN_PASSWORD)),
            )
        # Make sure no other account holds admin rights.
        conn.execute("UPDATE users SET is_admin = 0 WHERE username <> ?", (ADMIN_USERNAME,))
        conn.commit()


init_db()
seed_admin()


# ---------------------------------------------------------------------------
# Auth helpers (session-based)
# ---------------------------------------------------------------------------
def get_current_user() -> sqlite3.Row | None:
    user = getattr(g, "_current_user", "unset")
    if user != "unset":
        return user
    uid = session.get("user_id")
    if not uid:
        g._current_user = None
        return None
    row = get_db().execute(
        "SELECT id, username, email, is_admin, created_at FROM users WHERE id = ?",
        (uid,),
    ).fetchone()
    g._current_user = row
    return row


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not get_current_user():
            if request.method == "GET":
                return redirect(url_for("auth", mode="login", next=request.path))
            return jsonify({
                "ok": False,
                "error": "Bạn cần đăng nhập VSTORE trước khi quét.",
                "need_login": True,
            }), 401
        return view(*a, **kw)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        user = get_current_user()
        if not user:
            return redirect(url_for("auth", mode="login", next=request.path))
        if not user["is_admin"]:
            abort(403)
        return view(*a, **kw)
    return wrapper


def ensure_csrf() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def verify_csrf() -> bool:
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    return bool(sent) and secrets.compare_digest(sent, session.get("csrf_token", ""))


# Lightweight in-memory rate limiter (per-process). For multi-worker prod use a
# shared store (Redis); this still blunts single-host brute force well.
_ATTEMPTS: dict[str, list[float]] = {}
_ATTEMPTS_LOCK = threading.Lock()


def _client_ip() -> str:
    # ProxyFix populates remote_addr from X-Forwarded-For in prod.
    return request.remote_addr or "unknown"


def rate_limited(key: str, *, max_attempts: int, window_sec: int) -> bool:
    """True if `key` already has >= max_attempts within the window."""
    now = time.time()
    with _ATTEMPTS_LOCK:
        hits = [t for t in _ATTEMPTS.get(key, []) if now - t < window_sec]
        _ATTEMPTS[key] = hits
        return len(hits) >= max_attempts


def rate_record(key: str) -> None:
    with _ATTEMPTS_LOCK:
        _ATTEMPTS.setdefault(key, []).append(time.time())


@app.template_filter("vp_fmt")
def vp_fmt(value) -> str:
    """Format VP amount with thousands separator (e.g. 5350 -> 5,350)."""
    if value is None:
        return ""
    return f"{int(value):,}"


def _csp_nonce() -> str:
    """Per-request nonce for inline <script> tags (so we avoid 'unsafe-inline')."""
    nonce = getattr(g, "_csp_nonce", None)
    if nonce is None:
        nonce = secrets.token_urlsafe(16)
        g._csp_nonce = nonce
    return nonce


@app.context_processor
def inject_globals():
    """Make current_user + csrf_token + csp nonce available in every template."""
    return {
        "current_user": get_current_user(),
        "csrf_token":   ensure_csrf(),
        "csp_nonce":    _csp_nonce(),
    }


@app.after_request
def set_security_headers(resp):
    """Defense-in-depth response headers (clickjacking, sniffing, XSS, leaks)."""
    nonce = getattr(g, "_csp_nonce", None) or ""
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        # inline style attributes are used widely → allow inline styles + Google Fonts CSS
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        # skin/rank/card art comes from Riot + valorant-api over https; data: for tiny inline svg
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "upgrade-insecure-requests"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), payment=()"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    # Don't let the inspector image / pages get cached with credentials by shared caches.
    resp.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    return resp


def _status_for_riot_auth_error(e: RiotAuthError) -> int:
    return {
        "bad_input": 400,
        "bad_token": 400,
        "no_token": 400,
        "invalid_redirect_scheme": 400,
        "invalid_redirect_host": 400,
        "invalid_redirect_url": 400,
        "ssrf_blocked": 400,
        "unauthorized": 401,
        "forbidden": 403,
        "rate_limited": 429,
        "upstream_timeout": 504,
        "upstream_error": 502,
        "riot_api": 502,
        "scan_failed": 502,
    }.get(getattr(e, "code", "bad_input"), 400)


@app.errorhandler(RiotAuthError)
def handle_riot_auth_error(exc: RiotAuthError):
    return jsonify({
        "ok": False,
        "error": str(exc),
        "code": getattr(exc, "code", "auth_failed"),
    }), _status_for_riot_auth_error(exc)






# ---------------------------------------------------------------------------
# Showcase skin catalog (hydrated from valorant-api.com)
# ---------------------------------------------------------------------------
SHOWCASE_SKINS = [
    {"weapon": "Vandal",   "skin": "Reaver Vandal",            "_search_name": "Reaver Vandal",
     "edition": "VP · Premium",  "tier": "vp",        "tone": "violet",  "owned": True,  "image": None},
    {"weapon": "Phantom",  "skin": "Prime//2.0 Phantom",       "_search_name": "Prime//2.0 Phantom",
     "edition": "VP · Premium",  "tier": "vp",        "tone": "gold",    "owned": True,  "image": None},
    {"weapon": "Operator", "skin": "Glitchpop Operator",       "_search_name": "Glitchpop Operator",
     "edition": "VP · Premium",  "tier": "vp",        "tone": "magenta", "owned": True,  "image": None},
    {"weapon": "Sheriff",  "skin": "Ion Sheriff",              "_search_name": "Ion Sheriff",
     "edition": "VP · Premium",  "tier": "vp",        "tone": "cyan",    "owned": True,  "image": None},
    {"weapon": "Phantom",  "skin": "Oni Phantom",              "_search_name": "Oni Phantom",
     "edition": "VP · Premium",   "tier": "vp",    "tone": "rose",    "owned": True,  "image": None},
    {"weapon": "Karambit", "skin": "Champions 2021 Karambit",  "_search_name": "Champions 2021 Karambit",
     "edition": "VP · Exclusive", "tier": "exclusive", "tone": "amber", "owned": True, "image": None,
     "price_vp": 5350},
]


def _hydrate_showcase_images() -> None:
    url = "https://valorant-api.com/v1/weapons/skins?language=en-US"
    try:
        req = Request(url, headers={"User-Agent": "VSTORE/1.0 (+meefushop)"})
        with urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, json.JSONDecodeError, TimeoutError, Exception):
        return

    by_name = {(s.get("displayName") or "").lower(): s for s in payload.get("data") or []}
    for skin in SHOWCASE_SKINS:
        match = by_name.get(skin["_search_name"].lower())
        if not match:
            continue
        chromas = match.get("chromas") or []
        image_url = None
        if chromas:
            image_url = chromas[0].get("fullRender") or chromas[0].get("displayIcon")
        skin["image"] = image_url or match.get("displayIcon")


threading.Thread(target=_hydrate_showcase_images, daemon=True).start()


def _warmup_riot_catalogs() -> None:
    """Pre-load all Riot API catalogs into memory cache at startup.
    This means the first real user request doesn't pay the cold-start penalty.
    """
    import riot_skins as _rs
    try:
        _rs.get_competitive_tier_catalog()
        _rs.get_content_tier_catalog()
        _rs.get_player_card_catalog()
        _rs.get_seasons_catalog()
        _rs.get_currency_catalog()
    except Exception:
        pass  # non-fatal — catalogs will load on-demand if warmup fails


threading.Thread(target=_warmup_riot_catalogs, daemon=True).start()


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@app.route("/2fa")
def tfa_page():
    return render_template("tfa.html", active_tab="2fa")

@app.route("/api/2fa/generate", methods=["POST"])
def tfa_generate():
    import pyotp, base64
    data = request.get_json(silent=True) or {}
    secret = (data.get("secret") or "").strip().replace(" ", "").upper()
    if not secret:
        return jsonify({"error": "Thiếu secret key."}), 400
    # Pad base32 if needed
    pad = len(secret) % 8
    if pad:
        secret += "=" * (8 - pad)
    try:
        totp = pyotp.TOTP(secret)
        otp = totp.now()
        remaining = 30 - (int(time.time()) % 30)
        return jsonify({"otp": otp, "remaining": remaining})
    except Exception as e:
        return jsonify({"error": f"Secret key không hợp lệ: {e}"}), 400

@app.route("/")
def index():
    from riot_auth import RIOT_LOGIN_URL, RIOT_LOGOUT_URL
    return render_template(
        "index.html",
        active_tab="home",
        skins=SHOWCASE_SKINS,
        riot_login_url=RIOT_LOGIN_URL,
        riot_logout_url=RIOT_LOGOUT_URL,
    )


TIER_COLOR_MAP = {
    "12683d76-48d7-840a-f0f4-c085b1350850": "#5a9fe2",   # Select (Blue)
    "0cebbd32-4112-6ad1-218d-944d47e326e2": "#00bda2",   # Deluxe (Green)
    "607fae73-81a2-47a6-bc0d-47ef9d21a487": "#d1548d",   # Premium (Pink/Magenta)
    "11e0f608-43f1-acf2-c973-158a7e584f5c": "#f1b82d",   # Ultra (Yellow)
    "e04bde6b-4369-969c-73b5-0c937040b128": "#ff8c46",   # Exclusive (Orange)
}


DEFAULT_TIER_ICONS = {
    "exclusive_ultra": "https://media.valorant-api.com/contenttiers/e046854e-406c-37f4-6607-19a9ba8426fc/displayicon.png",
    "premium": "https://media.valorant-api.com/contenttiers/60bca009-4182-7998-dee7-b8a2558dc369/displayicon.png",
    "deluxe": "https://media.valorant-api.com/contenttiers/0cebb8be-46d7-c12a-d306-e9907bfc5a25/displayicon.png",
    "select": "https://media.valorant-api.com/contenttiers/12683d76-48a3-4e09-6985794f0445/displayicon.png",
}


def _classify_skin_tier(row: dict, norm_ctiers: dict) -> str:
    tuuid = (row.get("content_tier_uuid") or "").lower()
    ctier = norm_ctiers.get(tuuid) if tuuid else None

    if ctier:
        dev = ((ctier.get("dev_name") or "") + " " + (ctier.get("name") or "")).lower()
        if "exclusive" in dev or "ultra" in dev:
            return "exclusive_ultra"
        if "premium" in dev:
            return "premium"
        if "deluxe" in dev:
            return "deluxe"
        if "select" in dev:
            return "select"

    if tuuid in ("e04bde6b-4369-969c-73b5-0c937040b128", "e046854e-406c-37f4-6607-19a9ba8426fc", "11e0f608-43f1-acf2-c973-158a7e584f5c", "60bca009-4182-7998-dee7-b8a2558dc369"):
        return "exclusive_ultra"
    if tuuid in ("607fae73-81a2-47a6-bc0d-47ef9d21a487",):
        return "premium"
    if tuuid in ("0cebbd32-4112-6ad1-218d-944d47e326e2", "0cebb8be-46d7-c12a-d306-e9907bfc5a25"):
        return "deluxe"
    if tuuid in ("12683d76-48d7-840a-f0f4-c085b1350850", "12683d76-48a3-4e09-6985794f0445"):
        return "select"

    price = row.get("price_vp") or 0
    if price >= 2175:
        return "exclusive_ultra"
    elif price >= 1775:
        return "premium"
    elif price >= 1275:
        return "deluxe"
    elif price >= 875:
        return "select"

    if row.get("is_vp_skin"):
        return "exclusive_ultra"

    return ""


def _get_cached_catalogs():
    """Return all Riot catalogs, cached on Flask g for the lifetime of the request.
    This avoids calling 5 separate catalog functions (each doing its own dict lookup)
    on every single page render.
    """
    cached = getattr(g, "_riot_catalogs", None)
    if cached:
        return cached
    import riot_skins as _rs
    catalogs = {
        "tier":         _rs.get_competitive_tier_catalog() or {},
        "card":         _rs.get_player_card_catalog() or {},
        "currency":     _rs.get_currency_catalog() or {},
        "content_tier": _rs.get_content_tier_catalog() or {},
        "season":       _rs.get_seasons_catalog() or {"latest_act": None, "seasons": {}},
    }
    g._riot_catalogs = catalogs
    return catalogs


def build_html_card_context(scan_row: sqlite3.Row | None, skip_price_fetch: bool = False) -> dict:
    if not scan_row:
        return {}
    import riot_skins
    try:
        account_info = json.loads(scan_row["account_info_json"] or "{}")
    except Exception:
        account_info = {}
    try:
        skin_payload = json.loads(scan_row["skin_list_json"] or "{}")
    except Exception:
        skin_payload = {}

    filtered_rows = riot_skins.resolve_selected_rows(skin_payload)

    # ── Use g-level catalog cache so we don't pay 5 dict-lookups per request ──
    catalogs = _get_cached_catalogs()
    tier_catalog         = catalogs["tier"]
    card_catalog         = catalogs["card"]
    currency_catalog     = catalogs["currency"]
    content_tier_catalog = catalogs["content_tier"]
    season_catalog       = catalogs["season"]

    norm_ctiers = {k.lower(): v for k, v in content_tier_catalog.items()} if content_tier_catalog else {}

    identity = (account_info.get("loadout") or {}).get("Identity") or {}
    card_id = identity.get("PlayerCardID")
    card_meta = card_catalog.get(card_id) if card_id else None

    raw_rank_tier = account_info.get("rank_tier") or 0
    try:
        rank_tier = int(raw_rank_tier)
    except (ValueError, TypeError):
        rank_tier = 0
    rank_meta = (
        tier_catalog.get(rank_tier)
        or tier_catalog.get(str(rank_tier))
        or tier_catalog.get(0)
        or {"name": "UNRATED", "color": "ffffff", "icon": None}
    )
    act_name = riot_skins.format_act_name(season_catalog.get("latest_act"), season_catalog.get("seasons"))


    tier_counts = {
        "exclusive_ultra": 0,
        "premium": 0,
        "deluxe": 0,
        "select": 0,
    }
    tier_icons = dict(DEFAULT_TIER_ICONS)

    for tuuid, tinfo in norm_ctiers.items():
        dev = ((tinfo.get("dev_name") or "") + " " + (tinfo.get("name") or "")).lower()
        icon = tinfo.get("icon")
        if not icon:
            continue
        if "exclusive" in dev or "ultra" in dev:
            tier_icons["exclusive_ultra"] = icon
        elif "premium" in dev:
            tier_icons["premium"] = icon
        elif "deluxe" in dev:
            tier_icons["deluxe"] = icon
        elif "select" in dev:
            tier_icons["select"] = icon

    CATEGORY_TIER_COLORS = {
        "exclusive_ultra": "#f5955b",
        "premium": "#d1548d",
        "deluxe": "#00bda2",
        "select": "#5a9fe2",
    }

    for row in filtered_rows:
        tuuid = (row.get("content_tier_uuid") or "").lower()
        ctier = norm_ctiers.get(tuuid) if tuuid else None
        if ctier and ctier.get("icon"):
            row["tier_icon"] = ctier["icon"]

        cat = _classify_skin_tier(row, norm_ctiers)
        row["tier_color"] = TIER_COLOR_MAP.get(tuuid) or CATEGORY_TIER_COLORS.get(cat) or "#f5955b"

        if cat in tier_counts:
            tier_counts[cat] += 1
            if cat in DEFAULT_TIER_ICONS and not row.get("tier_icon"):
                row["tier_icon"] = DEFAULT_TIER_ICONS[cat]
            if ctier and ctier.get("icon"):
                tier_icons[cat] = ctier["icon"]

    WEAPON_TYPES = ["Melee", "Vandal", "Phantom", "Operator", "Ghost", "Sheriff", "Classic", "Guardian", "Odin", "Spectre", "Ares", "Judge", "Bucky", "Bulldog", "Stinger", "Marshal", "Shorty", "Outlaw"]
    for row in filtered_rows:
        wname = row.get("weapon_name") or row.get("name") or ""
        cat = "Other"
        for wt in WEAPON_TYPES:
            if wt.lower() in wname.lower():
                cat = wt
                break
        if cat == "Other" and any(k in wname.lower() for k in ["knife", "blade", "sword", "dagger", "axe", "hammer", "scythe", "karambit", "katar", "baton"]):
            cat = "Melee"
        row["weapon_cat"] = cat

    # ── Price enrichment ──
    price_map = {}
    if not skip_price_fetch:
        try:
            shard = scan_row["shard"] if "shard" in scan_row.keys() else None
            access_token = scan_row["access_token"] if "access_token" in scan_row.keys() else None
            entitlements_token = scan_row["entitlements_token"] if "entitlements_token" in scan_row.keys() else None
            if shard and access_token and entitlements_token:
                price_map = riot_skins.fetch_offer_prices(shard, access_token, entitlements_token)
        except Exception:
            price_map = {}

    riot_skins.enrich_skin_rows_with_prices(filtered_rows, price_map, catalog=None)

    counts = account_info.get("counts") or {}
    wallet = account_info.get("wallet") or {}

    shard = scan_row["shard"] if "shard" in scan_row.keys() else None
    puuid = scan_row["puuid"] if "puuid" in scan_row.keys() else None
    access_token = scan_row["access_token"] if "access_token" in scan_row.keys() else None
    entitlements_token = scan_row["entitlements_token"] if "entitlements_token" in scan_row.keys() else None

    buddies_list = skin_payload.get("buddies") or []
    if not buddies_list:
        buddy_ids = skin_payload.get("owned_buddy_ids")
        if not buddy_ids and shard and puuid and access_token and entitlements_token:
            try:
                buddy_ids = riot_skins.fetch_owned_item_ids(shard, puuid, riot_skins.ITEM_TYPE_BUDDIES, access_token, entitlements_token)
            except Exception:
                buddy_ids = []
        if buddy_ids:
            buddies_list = riot_skins.resolve_owned_buddy_rows(buddy_ids)

    cards_list = skin_payload.get("cards") or []
    card_ids = skin_payload.get("owned_card_ids")
    if not card_ids and shard and puuid and access_token and entitlements_token:
        try:
            card_ids = riot_skins.fetch_owned_item_ids(shard, puuid, riot_skins.ITEM_TYPE_CARDS, access_token, entitlements_token)
        except Exception:
            card_ids = []
    if card_ids:
        cards_list = riot_skins.resolve_owned_card_rows(card_ids)


    sprays_list = skin_payload.get("sprays") or []
    if not sprays_list:
        spray_ids = skin_payload.get("owned_spray_ids")
        if not spray_ids and shard and puuid and access_token and entitlements_token:
            try:
                spray_ids = riot_skins.fetch_owned_item_ids(shard, puuid, riot_skins.ITEM_TYPE_SPRAYS, access_token, entitlements_token)
            except Exception:
                spray_ids = []
        if spray_ids:
            sprays_list = riot_skins.resolve_owned_spray_rows(spray_ids)

    agents_list = skin_payload.get("agents") or []
    if not agents_list:
        agent_ids = skin_payload.get("owned_agent_ids")
        if not agent_ids and shard and puuid and access_token and entitlements_token:
            try:
                agent_ids = riot_skins.fetch_owned_item_ids(shard, puuid, riot_skins.ITEM_TYPE_AGENTS, access_token, entitlements_token)
            except Exception:
                agent_ids = []
        if agent_ids:
            agents_list = riot_skins.resolve_owned_agent_rows(agent_ids)



    card_data = {
        "display_name": account_info.get("display_name") or scan_row["display_name"] or scan_row["riot_username"] or "Player",
        "shard": scan_row["shard"] or "ap",
        "account_level": account_info.get("account_level") or identity.get("AccountLevel") or 0,
        "ranked_rating": account_info.get("ranked_rating") or 0,
        "skins_count": len(filtered_rows),
        "total_skins": skin_payload.get("total_skins", len(filtered_rows)),
        "updated_at": scan_row["created_at"],
        "counts": {
            "agents": counts.get("agents") or len(agents_list),
            "sprays": counts.get("sprays") or len(sprays_list),
            "buddies": counts.get("buddies") or len(buddies_list),
            "cards": counts.get("cards") or len(cards_list),
        },
        "wallet": {
            "vp": wallet.get("vp", 0),
            "rp": wallet.get("rp", 0),
            "kc": wallet.get("kc", 0),
        }
    }


    currencies = {
        "vp_icon": currency_catalog.get(riot_skins.CURRENCY_VP),
        "rp_icon": currency_catalog.get(riot_skins.CURRENCY_RP),
        "kc_icon": currency_catalog.get(riot_skins.CURRENCY_KC),
    }

    return {
        "card_data": card_data,
        "filtered_rows": filtered_rows,
        "rank_meta": rank_meta,
        "card_meta": card_meta,
        "act_name": act_name,
        "tier_counts": tier_counts,
        "tier_icons": tier_icons,
        "currencies": currencies,
        "buddies_list": buddies_list,
        "cards_list": cards_list,
        "sprays_list": sprays_list,
        "agents_list": agents_list,
    }


@app.route("/image")
@app.route("/image/<job>")
def inspector(job: str | None = None):
    user = get_current_user()
    scans: list[sqlite3.Row] = []
    active_scan = None
    skin_summary = None
    card_ctx = {}

    if user:
        # Lightweight query for the sidebar history list — exclude heavy JSON blobs
        scans = get_db().execute(
            "SELECT job_id, riot_username, display_name, shard, skins_found, "
            "skins_total, image_url, created_at "
            "FROM scans WHERE user_id = ? ORDER BY created_at DESC LIMIT 30",
            (user["id"],),
        ).fetchall()
        target_job = job or (scans[0]["job_id"] if scans else None)
        if target_job:
            # Full SELECT for active scan (includes JSON blobs needed for card render)
            is_admin = bool(user["is_admin"])
            if is_admin:
                # Admin can view any scan regardless of owner
                active_scan = get_db().execute(
                    "SELECT * FROM scans WHERE job_id = ?",
                    (target_job,),
                ).fetchone()
            else:
                active_scan = get_db().execute(
                    "SELECT * FROM scans WHERE user_id = ? AND job_id = ?",
                    (user["id"], target_job),
                ).fetchone()
            if not active_scan and job:
                # Job not found or not owned by current user → 404
                abort(404)

            if active_scan:
                skin_summary = _skin_summary(active_scan)
                # skip_price_fetch=True: prices already stored in skin_list_json,
                # avoids a potentially-blocking Riot API roundtrip on every page load.
                card_ctx = build_html_card_context(active_scan, skip_price_fetch=True)

    return render_template(
        "inspector.html",
        active_tab="inspector",
        scans=scans,
        active_scan=active_scan,
        skin_summary=skin_summary,
        **card_ctx,
    )




def _skin_summary(scan_row: sqlite3.Row) -> dict:
    """Parse a scan's skin_list_json into counts for the inspector UI."""
    try:
        payload = json.loads(scan_row["skin_list_json"] or "{}")
    except Exception:
        payload = {}
    all_skins = payload.get("skins", []) or []
    selected = payload.get("selected_skin_ids")
    if selected is None:
        selected = [s.get("uuid") for s in all_skins]
    return {
        "total":       payload.get("total_skins", len(all_skins)),
        "total_vp":    payload.get("total_vp_skins", 0),
        "selected":    len(selected),
        "has_payload": bool(all_skins),
    }


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/auth")
def auth():
    mode = (request.args.get("mode") or "login").lower()
    if mode not in ("login", "register"):
        mode = "login"
    if get_current_user():
        return redirect(url_for("inspector"))
    return render_template(
        "auth.html",
        mode=mode,
        next_url=request.args.get("next", ""),
        form={},
    )


@app.post("/register")
def register():
    if not verify_csrf():
        flash("CSRF token không hợp lệ. Vui lòng thử lại.", "error")
        return redirect(url_for("auth", mode="register"))

    username = (request.form.get("username") or "").strip()
    email    = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    tos      = request.form.get("accept_tos") == "1"
    next_url = request.form.get("next") or ""

    errors: list[str] = []
    if not USERNAME_RE.match(username):
        errors.append("Username 3-24 ký tự, chỉ chữ / số / _ / -")
    if not email:
        errors.append("Email là bắt buộc.")
    elif "@" not in email or "." not in email.split("@")[-1]:
        errors.append("Email không hợp lệ.")
    if len(password) < PASSWORD_MIN:
        errors.append(f"Mật khẩu cần tối thiểu {PASSWORD_MIN} ký tự.")
    if not tos:
        errors.append("Bạn cần đồng ý điều khoản sử dụng.")

    if errors:
        for e in errors:
            flash(e, "error")
        return render_template(
            "auth.html",
            mode="register",
            next_url=next_url,
            form={"username": username, "email": email or ""},
        ), 400

    db = get_db()
    existing = db.execute(
        "SELECT id FROM users WHERE LOWER(username) = LOWER(?) OR (email IS NOT NULL AND email = ?)",
        (username, email),
    ).fetchone()
    if existing:
        flash("Username hoặc email đã được dùng.", "error")
        return render_template(
            "auth.html",
            mode="register",
            next_url=next_url,
            form={"username": username, "email": email or ""},
        ), 409

    db.execute(
        "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
        (username, email, generate_password_hash(password)),
    )
    db.commit()

    row = db.execute(
        "SELECT id FROM users WHERE LOWER(username) = LOWER(?)",
        (username,),
    ).fetchone()
    session.clear()
    session.permanent = True
    session["user_id"] = row["id"]
    ensure_csrf()

    flash(f"Chào mừng, {username}! Bắt đầu quét nào.", "success")
    return redirect(next_url or url_for("index"))


@app.post("/login")
def login():
    if not verify_csrf():
        flash("CSRF token không hợp lệ. Vui lòng thử lại.", "error")
        return redirect(url_for("auth", mode="login"))

    # Brute-force throttle: max 8 failed attempts / 5 min per IP.
    ip_key = f"login:{_client_ip()}"
    if rate_limited(ip_key, max_attempts=8, window_sec=300):
        flash("Quá nhiều lần đăng nhập sai. Vui lòng đợi vài phút rồi thử lại.", "error")
        return redirect(url_for("auth", mode="login"))

    identifier = (request.form.get("identifier") or "").strip()
    password   = request.form.get("password") or ""
    remember   = request.form.get("remember") == "1"
    next_url   = request.form.get("next") or ""

    if not identifier or not password:
        flash("Nhập đủ username/email + mật khẩu.", "error")
        return redirect(url_for("auth", mode="login"))

    row = get_db().execute(
        "SELECT id, username, password_hash FROM users "
        "WHERE LOWER(username) = LOWER(?) OR email = LOWER(?)",
        (identifier, identifier),
    ).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        rate_record(ip_key)
        flash("Sai username hoặc mật khẩu.", "error")
        return render_template(
            "auth.html",
            mode="login",
            next_url=next_url,
            form={"identifier": identifier},
        ), 401

    get_db().execute(
        "UPDATE users SET last_login_at = datetime('now') WHERE id = ?",
        (row["id"],),
    )
    get_db().commit()

    session.clear()
    session.permanent = remember
    session["user_id"] = row["id"]
    ensure_csrf()

    flash(f"Đăng nhập thành công.", "success")
    return redirect(next_url or url_for("index"))


@app.post("/logout")
def logout():
    if not verify_csrf():
        flash("CSRF token không hợp lệ.", "error")
        return redirect(url_for("index"))
    session.clear()
    flash("Đã đăng xuất.", "success")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Scan API
# ---------------------------------------------------------------------------
def _count_scans_last_24h(user_id: int) -> int:
    row = get_db().execute(
        "SELECT COUNT(*) AS n FROM scans "
        "WHERE user_id = ? AND created_at >= datetime('now', '-24 hours')",
        (user_id,),
    ).fetchone()
    return int(row["n"] or 0)


@app.post("/api/scan")
@login_required
def api_scan():
    user = get_current_user()
    payload = request.get_json(silent=True) or {}

    # CSRF for JSON: accept either form-style token or header.
    sent_csrf = payload.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not sent_csrf or not secrets.compare_digest(sent_csrf, session.get("csrf_token", "")):
        return jsonify({"ok": False, "error": "CSRF token không hợp lệ."}), 403

    redirect_url = (payload.get("riot_redirect_url") or "").strip()
    region   = (payload.get("region") or "ap").strip().lower()
    include_bp = bool(payload.get("include_battlepass"))

    if not redirect_url:
        return jsonify({
            "ok": False,
            "error": "Vui lòng dán link redirect Riot (localhost/redirect#access_token=...).",
        }), 400
    if region not in REGION_TO_SHARD:
        return jsonify({"ok": False, "error": "Region không được hỗ trợ."}), 400

    # Rate limit per user (admin exempt)
    if not user["is_admin"] and _count_scans_last_24h(user["id"]) >= SCANS_PER_DAY_LIMIT:
        return jsonify({
            "ok": False,
            "error": f"Bạn đã dùng hết {SCANS_PER_DAY_LIMIT} lượt scan trong 24h. Quay lại sau.",
        }), 429

    shard = REGION_TO_SHARD[region]

    from riot_auth import RiotAuthError, riot_login_from_redirect

    try:
        result = riot_login_from_redirect(redirect_url, shard=shard)
    except RiotAuthError as e:
        return jsonify({"ok": False, "error": str(e), "code": e.code}), _status_for_riot_auth_error(e)
    except Exception as e:
        # Any unexpected failure — surface a short summary, log full to server.
        app.logger.exception("riot_login_from_redirect failed")
        return jsonify({
            "ok": False,
            "error": f"Lỗi không xác định: {type(e).__name__}: {e}",
            "code": "unexpected",
        }), 500

    access_token       = result.get("access_token") or ""
    entitlements_token = result.get("entitlements_token") or ""
    puuid              = result.get("puuid") or ""
    display_name       = result.get("display_name") or puuid or "player"
    login_username     = result.get("username") or ""   # lol[0].uname from id_token

    # --- Fetch inventory + render the inspector image -----------------------
    import riot_skins
    try:
        scan = riot_skins.build_account_scan(
            shard, puuid, access_token, entitlements_token,
            include_battlepass=include_bp,
        )
    except Exception as e:
        app.logger.exception("build_account_scan failed")
        return jsonify({
            "ok": False,
            "error": f"Đăng nhập OK nhưng quét kho skin lỗi: {type(e).__name__}: {e}",
            "code": "scan_failed",
        }), 502



    account_info = scan["account_info"]
    skin_payload = scan["skin_payload"]
    filtered     = scan["filtered_rows"]
    store        = scan.get("store") or {"items": [], "remaining": 0}
    display_name = account_info.get("display_name") or display_name

    job_id    = f"VS-{int(time.time())}-{secrets.token_hex(3)}"
    image_url = url_for("inspector", job=job_id)


    get_db().execute(
        "INSERT INTO scans (user_id, job_id, riot_username, display_name, puuid, "
        "region, shard, skins_found, skins_total, image_url, "
        "access_token, entitlements_token, skin_list_json, account_info_json, "
        "mask_riot_id, include_battlepass, store_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user["id"], job_id, login_username or display_name, display_name, puuid,
         region, shard, len(filtered), skin_payload["total_skins"], image_url,
         access_token, entitlements_token,
         json.dumps(skin_payload, ensure_ascii=False),
         json.dumps(account_info, ensure_ascii=False),
         0, 1 if include_bp else 0,
         json.dumps(store, ensure_ascii=False)),
    )
    get_db().commit()
    # -------------------------------------------------------------------------

    return jsonify({
        "ok":            True,
        "job_id":        job_id,
        "username":      display_name,
        "display_name":  display_name,
        "puuid":         puuid,
        "shard":         shard,
        "region":        region,
        "include_bp":    include_bp,
        "skins_found":   len(filtered),
        "skins_total":   skin_payload["total_skins"],
        "image_url":     image_url,
        "inspector_url": url_for("inspector", job=job_id),
    })


def _render_scan_image(job_id: str, account_info: dict, skin_rows: list) -> str | None:
    """Render the inspector PNG for a scan; return the static-relative URL."""
    import riot_skins
    if not riot_skins.PIL_AVAILABLE:
        return None
    fname = f"{job_id}.png"
    abs_path = str(UPLOAD_DIR / fname)
    try:
        ok = riot_skins.render_inspector_image(
            account_info, skin_rows, abs_path,
            logo_path=LOGO_PATH, banner_art_path=BANNER_ART_PATH)
    except Exception:
        app.logger.exception("render_inspector_image failed")
        return None
    return url_for("static", filename=f"uploads/{fname}") if ok else None


# ---------------------------------------------------------------------------
# Scan management (delete / refresh / rerender / select skins)
# ---------------------------------------------------------------------------
def _get_owned_scan(job_id: str) -> sqlite3.Row:
    """Return the scan row if it belongs to the current user (or admin), else 404."""
    user = get_current_user()
    if user and user["is_admin"]:
        row = get_db().execute(
            "SELECT * FROM scans WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    else:
        row = get_db().execute(
            "SELECT * FROM scans WHERE user_id = ? AND job_id = ?",
            (user["id"], job_id),
        ).fetchone()
    if not row:
        abort(404)
    return row


@app.route("/scan/<job_id>/card")
@login_required
def scan_card_standalone(job_id: str):
    scan = _get_owned_scan(job_id)
    ctx = build_html_card_context(scan)
    return render_template(
        "card.html",
        active_scan=scan,
        **ctx,
    )


@app.route("/scan/<job_id>/player_info")
@login_required
def scan_player_info(job_id: str):
    """Fetch Riot /userinfo and return parsed player info as JSON."""
    scan = _get_owned_scan(job_id)
    if not scan:
        return jsonify({"ok": False, "error": "Không tìm thấy scan."}), 404

    access_token = scan["access_token"] if "access_token" in scan.keys() else None
    if not access_token:
        return jsonify({"ok": False, "error": "Không có access token."}), 400

    try:
        from urllib.request import Request, urlopen
        from urllib.error import URLError
        req = Request(
            "https://auth.riotgames.com/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        with urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode())
    except Exception as e:
        # Fallback: build from stored account_info_json
        try:
            ai = json.loads(scan["account_info_json"] or "{}")
        except Exception:
            ai = {}
        raw = {}
        acct = ai.get("acct") or {}
        return jsonify({
            "ok": True,
            "cached": True,
            "riot_id": f"{acct.get('game_name', '')}#{acct.get('tag_line', '')}",
            "login": scan["riot_username"] or "",
            "email_verified": None,
            "ban_status": None,
            "country": ai.get("country", ""),
            "shard": scan["shard"] or "",
            "created_at": None,
            "puuid": scan["puuid"] or "",
        })

    acct   = raw.get("acct") or {}
    pw     = raw.get("pw") or {}
    affinity = raw.get("affinity") or {}

    # Riot affinity values: "as"=AP, "na"=NA, "eu"=EU, "kr"=KR
    AFFINITY_MAP = {"as": "AP", "na": "NA", "eu": "EU", "kr": "KR", "br": "BR", "latam": "LATAM", "ap": "AP"}
    # Determine shard from affinity
    raw_affinity = next(iter(affinity.values()), "") if affinity else scan["shard"] or ""
    affinity_region = AFFINITY_MAP.get(raw_affinity.lower(), raw_affinity.upper())

    # Ban/state
    state = acct.get("state", "")
    if state.upper() in ("", "ENABLED"):
        ban_text = "Không vi phạm"
        ban_ok   = True
    else:
        ban_text = state
        ban_ok   = False

    # ── Check ban via userinfo.ban.restrictions (more accurate than acct.state) ──
    # acct.state stays "ENABLED" even for Valorant-specific bans.
    # The real ban info is in raw["ban"]["restrictions"].
    BAN_TYPE_MAP = {
        "PERMANENT_BAN":             "Cấm vĩnh viễn",
        "TIME_BAN":                  "Cấm tạm thời",
        "COMPETITIVE_BAN":          "Cấm Xếp Hạng",
        "QUEUE_DELAY":              "Phạt chờ hàng chờ",
        "COMMUNICATION_RESTRICTION": "Cấm chat / voice",
        "WARNING":                  "Cảnh cáo",
    }
    BAN_REASON_MAP = {
        "THIRD_PARTY_TOOLS":  "phần mềm 3rd (hack/cheat)",
        "TOXICITY":           "ngôn ngữ xúc phạm / toxic",
        "CHEATING":           "gian lận",
        "BOOSTING":           "cày thuê (boost rank)",
        "AFK":                "AFK / bỏ trận",
        "AWAY_FROM_KEYBOARD": "AFK / treo máy",
        "LEAVING_GAME":       "thoát trận giữa chừng",
        "GAME_DISRUPTION":    "phá trận đấu",
    }
    ban_info = raw.get("ban") or {}
    restrictions = ban_info.get("restrictions") or []
    ban_level = "ok" if ban_ok else "danger"

    if restrictions:
        r0 = restrictions[0]
        btype  = r0.get("type", "BAN")
        reason = r0.get("reason", "")
        btype_vi  = BAN_TYPE_MAP.get(btype, btype)
        reason_vi = BAN_REASON_MAP.get(reason, reason)

        # Format expiration timestamp if present
        exp_ms = r0.get("expiration") or r0.get("expiresAt") or r0.get("expiry")
        exp_str = ""
        if exp_ms and isinstance(exp_ms, (int, float)):
            from datetime import datetime, timezone, timedelta
            exp_dt = datetime.fromtimestamp(exp_ms / 1000, tz=timezone(timedelta(hours=7)))
            exp_str = f" (đến {exp_dt.strftime('%H:%M %d/%m/%Y')})"

        ban_text = f"{btype_vi}" + (f" — {reason_vi}" if reason_vi else "") + exp_str
        ban_ok   = False
        ban_level = "warn" if btype in ("QUEUE_DELAY", "WARNING", "COMMUNICATION_RESTRICTION") else "danger"

    # Created at (ms → readable)
    created_ms = acct.get("created_at")
    if created_ms:
        from datetime import datetime, timezone
        created_dt = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
        created_str = created_dt.strftime("%d/%m/%Y")
    else:
        created_str = None

    return jsonify({
        "ok":            True,
        "cached":        False,
        "riot_id":       f"{acct.get('game_name', '')}#{acct.get('tag_line', '')}",
        "login":         scan["riot_username"] or "",
        "email_verified": raw.get("email_verified"),
        "phone_verified": raw.get("phone_number_verified"),
        "ban_status":    ban_text,
        "ban_ok":        ban_ok,
        "ban_level":     ban_level,
        "country":       raw.get("country", "").upper(),
        "shard":         affinity_region or (scan["shard"] or "").upper(),
        "created_at":    created_str,
        "puuid":         raw.get("sub") or scan["puuid"] or "",
        "player_locale": raw.get("player_locale") or "",
    })



def _render_card_png_bytes(html_content: str, base_url: str, clean_bg: bool = False) -> bytes | None:
    import base64, re
    from playwright.sync_api import sync_playwright

    style_css_path = APP_ROOT / "static" / "css" / "style.css"
    if style_css_path.exists():
        css_text = style_css_path.read_text(encoding="utf-8")
        html_content = html_content.replace('</head>', f'<style>{css_text}</style></head>')

    def _url_replacer(match):
        rel_path = match.group(1)
        full_path = APP_ROOT / "static" / rel_path
        if not full_path.exists():
            full_path = APP_ROOT / "assets" / Path(rel_path).name
        if full_path.exists():
            ext = full_path.suffix.lower()
            mime = "font/ttf" if ext in (".ttf", ".otf") else "image/png" if ext == ".png" else "image/jpeg" if ext in (".jpg", ".jpeg") else "application/octet-stream"
            b64 = base64.b64encode(full_path.read_bytes()).decode("ascii")
            return f"url('data:{mime};base64,{b64}')"
        return match.group(0)

    def _src_replacer(match):
        rel_path = match.group(1)
        full_path = APP_ROOT / "static" / rel_path
        if not full_path.exists():
            full_path = APP_ROOT / "assets" / Path(rel_path).name
        if full_path.exists():
            mime = "image/png" if full_path.suffix.lower() == ".png" else "image/jpeg"
            b64 = base64.b64encode(full_path.read_bytes()).decode("ascii")
            return f'src="data:{mime};base64,{b64}"'
        return match.group(0)

    html_content = re.sub(r'url\(["\']?/static/([^"\'\)]+)["\']?\)', _url_replacer, html_content)
    html_content = re.sub(r'src=["\']/static/([^"\']+)["\']', _src_replacer, html_content)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                base_url=base_url,
                viewport={"width": 1920, "height": 1400},
                device_scale_factor=2
            )
            page.set_content(html_content, wait_until="domcontentloaded", timeout=15000)
            page.evaluate("document.fonts.ready")

            try:
                card_height = page.evaluate("() => { const el = document.getElementById('inspector-card-canvas'); return el ? el.offsetHeight : 1400; }")
                if card_height and card_height > 800:
                    page.set_viewport_size({"width": 1920, "height": int(card_height) + 400})
            except Exception:
                pass

            page.evaluate("""
                async () => {
                    const imgs = Array.from(document.querySelectorAll('#inspector-card-canvas img'));
                    await Promise.race([
                        Promise.all(imgs.map(img => {
                            if (img.complete && img.naturalWidth !== 0) return Promise.resolve();
                            return new Promise(resolve => {
                                img.onload = resolve;
                                img.onerror = resolve;
                            });
                        })),
                        new Promise(resolve => setTimeout(resolve, 8000))
                    ]);
                }
            """)

            page.evaluate("document.querySelectorAll('.card-standalone-actions').forEach(el => el.remove())")

            if clean_bg:
                page.evaluate("""
                    document.body.style.background = 'transparent';
                    document.body.style.backgroundColor = 'transparent';
                    const canvas = document.getElementById('inspector-card-canvas');
                    if (canvas) {
                        canvas.style.background = 'transparent';
                        canvas.style.backgroundColor = 'transparent';
                        canvas.style.backgroundImage = 'none';
                        canvas.style.boxShadow = 'none';
                        canvas.style.border = 'none';
                        canvas.style.padding = '0';
                    }
                """)
            el = page.wait_for_selector("#inspector-card-canvas", timeout=10000)
            if not el:
                browser.close()
                return None
            png_bytes = el.screenshot(type="png", omit_background=True)
            browser.close()
            return png_bytes
    except Exception:
        app.logger.exception("Playwright render error in _render_card_png_bytes")
        return None


@app.route("/scan/<job_id>/export_png")
@login_required
def scan_export_png(job_id: str):
    import io
    scan = _get_owned_scan(job_id)
    ctx = build_html_card_context(scan)
    show_details = request.args.get("show_details") == "1" or request.args.get("show_names") == "1"
    account_price = request.args.get("price") or ""
    clean_bg = request.args.get("clean_bg") == "1"
    hide_banner = request.args.get("hide_banner") == "1"
    hide_profile = request.args.get("hide_profile") == "1"
    hide_stats = request.args.get("hide_stats") == "1"
    cols = request.args.get("cols") or "6"
    html_content = render_template(
        "card.html",
        active_scan=scan,
        show_details=show_details,
        account_price=account_price,
        clean_bg=clean_bg,
        hide_banner=hide_banner,
        hide_profile=hide_profile,
        hide_stats=hide_stats,
        cols=cols,
        **ctx
    )

    png_bytes = _render_card_png_bytes(html_content, request.host_url, clean_bg=clean_bg)
    if not png_bytes:
        flash("Lỗi render ảnh server-side.", "error")
        return redirect(url_for("inspector", job=job_id))

    raw_name = scan["display_name"] or "vstore_card"
    safe_name = re.sub(r'[^\w\-]+', '_', raw_name).strip('_')
    return send_file(
        io.BytesIO(png_bytes),
        mimetype="image/png",
        as_attachment=True,
        download_name=f"vstore-{safe_name}.png"
    )


# ---------------------------------------------------------------------------
# External Auto-Render API (Authenticated via API Key)
# ---------------------------------------------------------------------------
API_SECRET_KEY = "Nemchua3667!idkA"

@app.route("/api/auto_render", methods=["GET", "POST"])
@app.route("/api/v1/render", methods=["GET", "POST"])
def api_auto_render():
    import base64, io
    payload = request.get_json(silent=True) or {}
    
    # 1. Verify API Key
    provided_key = (
        request.headers.get("X-API-Key")
        or request.args.get("api_key")
        or payload.get("api_key")
        or request.form.get("api_key")
    )
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        provided_key = auth_header.split(" ", 1)[1].strip()

    if not provided_key or not secrets.compare_digest(provided_key, API_SECRET_KEY):
        return jsonify({"ok": False, "error": "Unauthorized: API Key không hợp lệ."}), 401

    # 2. Extract input token / redirect_url
    token_input = (
        payload.get("token")
        or payload.get("redirect_url")
        or payload.get("link")
        or payload.get("url")
        or request.form.get("token")
        or request.form.get("redirect_url")
        or request.form.get("link")
        or request.args.get("token")
        or request.args.get("redirect_url")
        or request.args.get("link")
        or ""
    ).strip()

    if not token_input:
        return jsonify({
            "ok": False,
            "error": "Thiếu token hoặc link redirect localhost (truyền parameter 'token' hoặc 'redirect_url')."
        }), 400

    region = (
        payload.get("region")
        or payload.get("shard")
        or request.form.get("region")
        or request.args.get("region")
        or "ap"
    ).strip().lower()

    # 3. Determine vp_only vs full mode
    mode = (
        payload.get("mode")
        or payload.get("option")
        or request.form.get("mode")
        or request.args.get("mode")
        or "vp_only"
    ).strip().lower()

    vp_only_param = payload.get("vp_only")
    if vp_only_param is None:
        vp_only_param = request.form.get("vp_only") or request.args.get("vp_only")

    if vp_only_param is not None:
        if str(vp_only_param).lower() in ("true", "1", "yes"):
            include_bp = False
            mode = "vp_only"
        elif str(vp_only_param).lower() in ("false", "0", "no"):
            include_bp = True
            mode = "full"
        else:
            include_bp = (mode != "full")
    else:
        include_bp = (mode == "full")

    shard = REGION_TO_SHARD.get(region, region if region in ("na", "eu", "ap", "kr", "pbe") else "ap")

    # 4. Authenticate with Riot & Scan Inventory
    from riot_auth import RiotAuthError, riot_login_from_redirect
    import riot_skins

    try:
        auth_result = riot_login_from_redirect(token_input, shard=shard)
    except RiotAuthError as e:
        return jsonify({"ok": False, "error": str(e), "code": e.code}), _status_for_riot_auth_error(e)
    except Exception as e:
        app.logger.exception("api_auto_render login failed")
        return jsonify({"ok": False, "error": f"Lỗi đăng nhập Riot: {type(e).__name__}: {e}"}), 500

    access_token       = auth_result.get("access_token") or ""
    entitlements_token = auth_result.get("entitlements_token") or ""
    puuid              = auth_result.get("puuid") or ""
    display_name       = auth_result.get("display_name") or puuid or "player"
    login_username     = auth_result.get("username") or ""

    try:
        scan_data = riot_skins.build_account_scan(
            shard, puuid, access_token, entitlements_token,
            include_battlepass=include_bp,
        )
    except Exception as e:
        app.logger.exception("api_auto_render scan failed")
        return jsonify({"ok": False, "error": f"Lỗi quét kho skin: {type(e).__name__}: {e}"}), 502



    account_info = scan_data["account_info"]
    skin_payload = scan_data["skin_payload"]
    filtered     = scan_data["filtered_rows"]
    store        = scan_data.get("store") or {"items": [], "remaining": 0}

    job_id    = f"VS-{int(time.time())}-{secrets.token_hex(3)}"
    image_url = url_for("inspector", job=job_id, _external=True)

    # Save to database
    db = get_db()
    system_user = db.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1").fetchone()
    user_id = system_user["id"] if system_user else 1

    db.execute(
        "INSERT INTO scans (user_id, job_id, riot_username, display_name, puuid, "
        "region, shard, skins_found, skins_total, image_url, "
        "access_token, entitlements_token, skin_list_json, account_info_json, "
        "mask_riot_id, include_battlepass, store_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, job_id, login_username or display_name, display_name, puuid,
         region, shard, len(filtered), skin_payload["total_skins"], image_url,
         access_token, entitlements_token,
         json.dumps(skin_payload, ensure_ascii=False),
         json.dumps(account_info, ensure_ascii=False),
         0, 1 if include_bp else 0,
         json.dumps(store, ensure_ascii=False)),
    )
    db.commit()

    scan_row = db.execute("SELECT * FROM scans WHERE job_id = ?", (job_id,)).fetchone()

    # 5. Extract metadata (shard, rank, agents)
    raw_rank_tier = account_info.get("rank_tier") or 0
    try:
        rank_tier = int(raw_rank_tier)
    except (ValueError, TypeError):
        rank_tier = 0

    tier_catalog = riot_skins.get_competitive_tier_catalog()
    rank_meta = (
        tier_catalog.get(rank_tier)
        or tier_catalog.get(str(rank_tier))
        or tier_catalog.get(0)
        or {}
    )
    raw_rank_name = rank_meta.get("name") or "Unranked"
    if not raw_rank_name or raw_rank_name.upper() in ("UNRATED", "UNRANKED", "0"):
        rank_name = "Unranked"
    else:
        rank_name = " ".join(word.capitalize() for word in raw_rank_name.split())


    agents_count = account_info.get("counts", {}).get("agents", 0)
    if not agents_count:
        try:
            buddies, cards, sprays, agents = riot_skins.resolve_owned_items(shard, puuid, access_token, entitlements_token)
            agents_count = len(agents)
        except Exception:
            agents_count = 0

    if agents_count >= 24:
        agents_str = "Full Agent"
    elif agents_count > 0:
        agents_str = f"{agents_count} Agents"
    else:
        agents_str = "0 Agents"

    shard_str = shard.lower()

    # 6. Render Card Image
    ctx = build_html_card_context(scan_row)
    html_content = render_template(
        "card.html",
        active_scan=scan_row,
        show_details=True,
        cols="6",
        **ctx
    )

    png_bytes = _render_card_png_bytes(html_content, request.host_url)

    png_saved_url = None
    b64_image = None

    if png_bytes:
        fname = f"{job_id}.png"
        abs_path = UPLOAD_DIR / fname
        abs_path.write_bytes(png_bytes)
        png_saved_url = url_for("static", filename=f"uploads/{fname}", _external=True)
        b64_image = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    # If raw image requested
    fmt = (payload.get("format") or request.args.get("format") or "json").lower()
    if fmt in ("image", "png", "file") and png_bytes:
        resp = send_file(
            io.BytesIO(png_bytes),
            mimetype="image/png",
            as_attachment=False,
            download_name=f"{job_id}.png"
        )
        resp.headers["X-Shard"] = shard_str
        resp.headers["X-Rank"] = rank_name
        resp.headers["X-Agents"] = agents_str
        resp.headers["X-Skins-Found"] = str(len(filtered))
        return resp

    return jsonify({
        "ok": True,
        "job_id": job_id,
        "shard": shard_str,
        "rank": rank_name,
        "agents": agents_str,
        "skins_found": len(filtered),
        "skins_total": skin_payload["total_skins"],
        "mode": mode,
        "image": png_saved_url or image_url,
        "image_url": png_saved_url or image_url,
        "image_base64": b64_image,
        "inspector_url": url_for("inspector", job=job_id, _external=True),
        "account": {
            "display_name": display_name,
            "level": account_info.get("account_level", 0),
            "vp": account_info.get("wallet", {}).get("vp", 0),
            "rp": account_info.get("wallet", {}).get("rp", 0),
            "kc": account_info.get("wallet", {}).get("kc", 0),
        }
    })




def _delete_scan_image(image_url: str | None) -> None:
    if not image_url:
        return
    fname = image_url.rsplit("/", 1)[-1]
    try:
        (UPLOAD_DIR / fname).unlink(missing_ok=True)
    except Exception:
        pass


def _scan_store(scan: sqlite3.Row) -> dict:
    """Parse a scan's stored daily-store snapshot (safe)."""
    try:
        return json.loads(scan["store_json"] or "{}") or {}
    except Exception:
        return {}


@app.post("/scan/<job_id>/delete")
@login_required
def scan_delete(job_id: str):
    if not verify_csrf():
        flash("CSRF token không hợp lệ.", "error")
        return redirect(url_for("inspector"))
    scan = _get_owned_scan(job_id)
    _delete_scan_image(scan["image_url"])
    get_db().execute("DELETE FROM scans WHERE id = ?", (scan["id"],))
    get_db().commit()
    flash("Đã xoá lần quét.")
    return redirect(url_for("inspector"))


@app.post("/scan/<job_id>/refresh")
@login_required
def scan_refresh(job_id: str):
    if not verify_csrf():
        flash("CSRF token không hợp lệ.", "error")
        return redirect(url_for("inspector", job=job_id))
    scan = _get_owned_scan(job_id)
    if not (scan["puuid"] and scan["access_token"] and scan["entitlements_token"]):
        flash("Thiếu token để refresh — hãy quét lại từ link Riot.", "error")
        return redirect(url_for("inspector", job=job_id))

    import riot_skins
    from riot_auth import RiotAuthError
    riot_skins.invalidate_skin_catalog()
    try:
        result = riot_skins.build_account_scan(
            scan["shard"], scan["puuid"], scan["access_token"], scan["entitlements_token"],
            include_battlepass=bool(scan["include_battlepass"]),
        )
    except RiotAuthError as e:
        flash(f"Refresh lỗi: {e} (token có thể đã hết hạn, hãy quét lại).", "error")
        return redirect(url_for("inspector", job=job_id))
    except Exception as e:
        flash(f"Refresh lỗi: {type(e).__name__}: {e}", "error")
        return redirect(url_for("inspector", job=job_id))

    account_info = result["account_info"]
    skin_payload = result["skin_payload"]
    filtered     = result["filtered_rows"]
    store        = result.get("store") or {"items": [], "remaining": 0}

    _delete_scan_image(scan["image_url"])
    image_url = _render_scan_image(job_id, account_info, filtered)
    get_db().execute(
        "UPDATE scans SET skins_found = ?, skins_total = ?, image_url = ?, "
        "skin_list_json = ?, account_info_json = ?, store_json = ?, display_name = ? WHERE id = ?",
        (len(filtered), skin_payload["total_skins"], image_url,
         json.dumps(skin_payload, ensure_ascii=False),
         json.dumps(account_info, ensure_ascii=False),
         json.dumps(store, ensure_ascii=False),
         account_info.get("display_name") or scan["display_name"], scan["id"]),
    )
    get_db().commit()
    flash(f"Refresh xong: {skin_payload['total_skins']} skin tổng, {len(filtered)} hiển thị.")
    return redirect(url_for("inspector", job=job_id))


@app.post("/scan/<job_id>/rerender")
@login_required
def scan_rerender(job_id: str):
    if not verify_csrf():
        flash("CSRF token không hợp lệ.", "error")
        return redirect(url_for("inspector", job=job_id))
    scan = _get_owned_scan(job_id)
    import riot_skins
    try:
        skin_payload = json.loads(scan["skin_list_json"] or "{}")
        account_info = json.loads(scan["account_info_json"] or "{}")
    except Exception:
        flash("Không có dữ liệu skin để render lại.", "error")
        return redirect(url_for("inspector", job=job_id))

    rows = riot_skins.resolve_selected_rows(skin_payload)
    account_info.setdefault("counts", {})["skins"] = len(rows)
    _delete_scan_image(scan["image_url"])
    image_url = _render_scan_image(job_id, account_info, rows)
    get_db().execute(
        "UPDATE scans SET image_url = ?, skins_found = ?, account_info_json = ? WHERE id = ?",
        (image_url, len(rows), json.dumps(account_info, ensure_ascii=False), scan["id"]),
    )
    get_db().commit()
    flash("Đã render lại ảnh.")
    return redirect(url_for("inspector", job=job_id))


@app.route("/scan/<job_id>/skins", methods=["GET", "POST"])
@login_required
def scan_skins(job_id: str):
    scan = _get_owned_scan(job_id)
    import riot_skins
    try:
        skin_payload = json.loads(scan["skin_list_json"] or "{}")
    except Exception:
        skin_payload = {}
    skins = skin_payload.get("skins", []) or []
    all_ids = [s.get("uuid") for s in skins if s.get("uuid")]
    selected_ids = skin_payload.get("selected_skin_ids") or all_ids
    selected_ids = [x for x in selected_ids if x in all_ids]

    if request.method == "POST":
        if not verify_csrf():
            flash("CSRF token không hợp lệ.", "error")
            return redirect(url_for("scan_skins", job_id=job_id))
        selected_ids = [x for x in request.form.getlist("selected_skin") if x in all_ids]
        rows = riot_skins.resolve_selected_rows(skin_payload, selected_ids)

        try:
            account_info = json.loads(scan["account_info_json"] or "{}")
        except Exception:
            account_info = {}
        account_info.setdefault("counts", {})["skins"] = len(rows)

        skin_payload["selected_skin_ids"] = selected_ids
        _delete_scan_image(scan["image_url"])
        image_url = _render_scan_image(job_id, account_info, rows)
        get_db().execute(
            "UPDATE scans SET skin_list_json = ?, account_info_json = ?, "
            "skins_found = ?, image_url = ? WHERE id = ?",
            (json.dumps(skin_payload, ensure_ascii=False),
             json.dumps(account_info, ensure_ascii=False),
             len(rows), image_url, scan["id"]),
        )
        get_db().commit()
        flash(f"Đã lưu {len(rows)} skin + render lại ảnh.")
        return redirect(url_for("scan_skins", job_id=job_id))

    return render_template(
        "skins.html",
        active_tab="inspector",
        scan=scan,
        skins=skins,
        total_skins=skin_payload.get("total_skins", len(skins)),
        total_vp_skins=skin_payload.get("total_vp_skins", 0),
        selected_skin_ids=selected_ids,
    )


@app.route("/scan/<job_id>/store")
@login_required
def scan_store(job_id: str):
    scan = _get_owned_scan(job_id)
    import riot_skins

    store, live, err = None, False, None
    if scan["puuid"] and scan["access_token"] and scan["entitlements_token"]:
        try:
            store = riot_skins.get_daily_store(
                scan["shard"], scan["puuid"], scan["access_token"], scan["entitlements_token"])
            live = True
            get_db().execute(
                "UPDATE scans SET store_json = ? WHERE id = ?",
                (json.dumps(store, ensure_ascii=False), scan["id"]),
            )
            get_db().commit()
        except Exception as e:
            err = f"{type(e).__name__}: {e}"

    if store is None:
        store = _scan_store(scan)

    now_utc = datetime.now(timezone.utc)
    next_reset = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    reset_ts = int(next_reset.timestamp())

    return render_template(
        "store.html",
        active_tab="inspector",
        scan=scan,
        items=store.get("items") or [],
        reset_ts=reset_ts,
        live=live,
        err=err,
    )


# ---------------------------------------------------------------------------
# Admin panel (only the seeded `admin` account)
# ---------------------------------------------------------------------------
@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    stats = {
        "users":  db.execute("SELECT COUNT(*) c FROM users WHERE is_admin = 0").fetchone()["c"],
        "scans":  db.execute("SELECT COUNT(*) c FROM scans").fetchone()["c"],
        "scans_24h": db.execute(
            "SELECT COUNT(*) c FROM scans WHERE created_at >= datetime('now','-24 hours')"
        ).fetchone()["c"],
        "skins":  db.execute("SELECT COALESCE(SUM(skins_found),0) c FROM scans").fetchone()["c"],
    }
    users = db.execute(
        "SELECT u.id, u.username, u.email, u.created_at, u.last_login_at, "
        "(SELECT COUNT(*) FROM scans s WHERE s.user_id = u.id) AS scan_count "
        "FROM users u WHERE u.is_admin = 0 ORDER BY u.created_at DESC LIMIT 200"
    ).fetchall()
    scans = db.execute(
        "SELECT s.job_id, s.display_name, s.shard, s.skins_found, s.skins_total, "
        "s.image_url, s.created_at, u.username "
        "FROM scans s JOIN users u ON u.id = s.user_id "
        "ORDER BY s.created_at DESC LIMIT 60"
    ).fetchall()
    return render_template(
        "admin.html",
        active_tab="admin",
        stats=stats,
        users=users,
        scans=scans,
    )



# ---------------------------------------------------------------------------
# LoL + TFT local module (isolated from Valorant inspector logic)
# Browser talks only to this Flask origin. Flask proxies to the local helper,
# so the existing CSP / Valorant render pipeline stays untouched.
# ---------------------------------------------------------------------------
@app.route("/lol-tft")
def lol_tft_collection():
    return render_template("lol_tft.html", active_tab="lol_tft")


@app.route("/lol-tft-helper/<path:helper_path>")
def lol_tft_helper_proxy(helper_path: str):
    allowed = {"api/collection", "asset", "health"}
    if helper_path not in allowed:
        abort(404)

    target = f"http://127.0.0.1:17321/{helper_path}"
    if request.query_string:
        target += "?" + request.query_string.decode("utf-8", errors="ignore")

    try:
        req = Request(target, headers={"User-Agent": "VSTORE-LoL-TFT-Proxy/100"})
        with urlopen(req, timeout=30) as upstream:
            body = upstream.read()
            status = int(getattr(upstream, "status", 200) or 200)
            ctype = upstream.headers.get("Content-Type", "application/octet-stream")
            return body, status, {
                "Content-Type": ctype,
                "Cache-Control": "no-store",
            }
    except Exception as exc:
        if helper_path.startswith("api/") or helper_path == "health":
            return jsonify({
                "ok": False,
                "error": "Không kết nối được Local Helper. Hãy chạy START_HELPER.bat và giữ League Client mở.",
                "detail": f"{type(exc).__name__}: {exc}",
            }), 502
        abort(502)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=36677, debug=False)
