"""OpenPing: tracked quote links, first-open owner ping, one approve-to-send follow-up."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import smtplib
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = APP_ROOT / "openping.db"
DEFAULT_UPLOAD = APP_ROOT / "uploads"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "openping-self-hosted-change-me")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
BOT_UA_RE = re.compile(
    r"bot|crawl|spider|slurp|facebookexternalhit|WhatsApp|Preview",
    re.IGNORECASE,
)
ALLOWED_EXT = {".pdf": "pdf", ".html": "html", ".htm": "html"}

STATUSES = (
    "draft",
    "link_ready",
    "sent",
    "opened",
    "followup_queued",
    "followup_sent",
    "skipped",
)

OPEN_ENDPOINTS = {
    "health",
    "login",
    "logout",
    "public_view",
    "tracking_pixel",
    "static",
}

# Tiny 1x1 transparent GIF
PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
    b"\x00\x00\x02\x02D\x01\x00;"
)

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()

def database_path() -> str:
    raw = _env("DATABASE_PATH")
    if raw:
        return raw
    return str(DEFAULT_DB)

def upload_dir() -> Path:
    raw = _env("UPLOAD_DIR")
    path = Path(raw) if raw else DEFAULT_UPLOAD
    path.mkdir(parents=True, exist_ok=True)
    return path

def smtp_configured() -> bool:
    return bool(_env("SMTP_HOST"))

def llm_configured() -> bool:
    if _env("OPENAI_API_KEY"):
        return True
    return bool(_env("LLM_API_KEY") and _env("LLM_BASE_URL"))

def owner_password() -> str:
    return os.environ.get("OWNER_PASSWORD", "").strip()

def public_base_url() -> str:
    return _env("PUBLIC_BASE_URL").rstrip("/")

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)

def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def utc_now_iso() -> str:
    return to_iso(utc_now())

def valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match((value or "").strip()))

def first_name(name: str) -> str:
    parts = (name or "").strip().split()
    return parts[0] if parts else "there"

def signoff_block() -> str:
    name = _env("FROM_NAME")
    email = _env("FROM_EMAIL")
    if name and email:
        return f"{name}\n{email}"
    if name:
        return name
    if email:
        return email
    business = _env("BUSINESS_NAME")
    if business:
        return business
    return "Your name"

def tracked_url(token: str) -> str:
    base = public_base_url() or "http://localhost:8080"
    return f"{base}/v/{token}"

def is_bot_ua(ua: str | None) -> bool:
    return bool(ua and BOT_UA_RE.search(ua))

def connect_db() -> sqlite3.Connection:
    path = database_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    db = sqlite3.connect(path, timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db

def get_db() -> sqlite3.Connection:
    db = getattr(g, "_db", None)
    if db is None:
        db = connect_db()
        g._db = db
    return db

@app.teardown_appcontext
def close_db(_exc: BaseException | None) -> None:
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()

def init_db() -> None:
    with app.app_context():
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                prospect_name TEXT NOT NULL,
                prospect_email TEXT NOT NULL,
                notes TEXT,
                token TEXT NOT NULL UNIQUE,
                file_path TEXT NOT NULL,
                file_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                open_count INTEGER NOT NULL DEFAULT 0,
                opened_at TEXT,
                link_sent_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS opens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_id INTEGER NOT NULL REFERENCES quotes(id),
                at TEXT NOT NULL,
                ip TEXT,
                user_agent TEXT,
                is_first INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS followups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_id INTEGER NOT NULL UNIQUE REFERENCES quotes(id),
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                sent_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_quotes_token ON quotes(token);
            CREATE INDEX IF NOT EXISTS idx_quotes_created_at ON quotes(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_opens_quote_id ON opens(quote_id);
            """
        )
        db.commit()

@app.context_processor
def inject_globals() -> dict:
    return {
        "marketing_url": _env("MARKETING_URL"),
        "smtp_configured": smtp_configured(),
        "llm_configured": llm_configured(),
        "business_name": _env("BUSINESS_NAME") or "OpenPing",
        "owner_locked": bool(owner_password()),
        "logged_in": bool(session.get("owner")) or not owner_password(),
    }

@app.before_request
def protect_owner_routes():
    if request.endpoint in OPEN_ENDPOINTS or request.endpoint is None:
        return None
    if not owner_password():
        return None
    if session.get("owner"):
        return None
    nxt = request.path if request.method == "GET" else "/"
    return redirect(url_for("login", next=nxt))

def secrets_equal(provided: str, expected: str) -> bool:
    import hmac

    a = (provided or "").encode("utf-8")
    b = (expected or "").encode("utf-8")
    if len(a) != len(b):
        return hmac.compare_digest(b, b) and False
    return hmac.compare_digest(a, b)

def _safe_next(val: str | None) -> str:
    raw = (val or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return url_for("index")

def get_quote(quote_id: int) -> sqlite3.Row | None:
    return get_db().execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()

def get_quote_by_token(token: str) -> sqlite3.Row | None:
    return get_db().execute("SELECT * FROM quotes WHERE token = ?", (token,)).fetchone()

def get_followup(quote_id: int) -> sqlite3.Row | None:
    return (
        get_db()
        .execute("SELECT * FROM followups WHERE quote_id = ?", (quote_id,))
        .fetchone()
    )

def send_smtp(to_email: str, subject: str, body: str) -> None:
    host 