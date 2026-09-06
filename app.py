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
    host = _env("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP is not configured.")
    from_email = _env("FROM_EMAIL")
    if not from_email:
        raise RuntimeError("FROM_EMAIL is required to send mail.")
    port = int(_env("SMTP_PORT") or "587")
    user = _env("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD", "")
    tls_raw = _env("SMTP_TLS") or "true"
    use_tls = tls_raw.lower() in {"1", "true", "yes", "on"}
    from_name = _env("FROM_NAME")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)

def suggested_send_subject(quote: sqlite3.Row | dict) -> str:
    title = quote["title"] if not isinstance(quote, dict) else quote["title"]
    business = _env("BUSINESS_NAME") or "us"
    return f"Your quote from {business}: {title}"

def suggested_send_body(quote: sqlite3.Row | dict) -> str:
    name = quote["prospect_name"] if not isinstance(quote, dict) else quote["prospect_name"]
    token = quote["token"] if not isinstance(quote, dict) else quote["token"]
    title = quote["title"] if not isinstance(quote, dict) else quote["title"]
    fn = first_name(name)
    business = _env("BUSINESS_NAME") or "us"
    link = tracked_url(token)
    sign = signoff_block()
    return (
        f"Hi {fn},\n\n"
        f"Thanks for the chance to put together the {title} quote. "
        f"I've posted it here so you can open it anytime:\n\n"
        f"{link}\n\n"
        f"Happy to walk through any questions.\n\n"
        f"Best,\n"
        f"{sign}\n"
    )

def followup_template_subject(quote: sqlite3.Row | dict) -> str:
    title = quote["title"] if not isinstance(quote, dict) else quote["title"]
    name = quote["prospect_name"] if not isinstance(quote, dict) else quote["prospect_name"]
    return f"Quick thought on the {title} quote, {first_name(name)}"

def followup_template_body(quote: sqlite3.Row | dict) -> str:
    name = quote["prospect_name"] if not isinstance(quote, dict) else quote["prospect_name"]
    title = quote["title"] if not isinstance(quote, dict) else quote["title"]
    fn = first_name(name)
    business = _env("BUSINESS_NAME") or "us"
    sign = signoff_block()
    return (
        f"Hi {fn},\n\n"
        f"I noticed you opened the {title} quote — hope it was clear. "
        f"If anything looks off or you want a quick call to walk through options, "
        f"just reply to this email.\n\n"
        f"Happy to adjust scope or timing so it fits.\n\n"
        f"Thanks,\n"
        f"{sign}\n"
        f"{business}\n"
    )

def _llm_api_details() -> tuple[str, str, str] | None:
    """Return (api_key, base_url, model) or None if unset."""
    openai_key = _env("OPENAI_API_KEY")
    if openai_key:
        return (
            openai_key,
            "https://api.openai.com/v1",
            _env("LLM_MODEL") or "gpt-4o-mini",
        )
    key = _env("LLM_API_KEY")
    base = _env("LLM_BASE_URL").rstrip("/")
    if key and base:
        return (key, base, _env("LLM_MODEL") or "gpt-4o-mini")
    return None

def maybe_llm_followup_body(quote: sqlite3.Row | dict, timeout: float = 3.0) -> str | None:
    """One model call for follow-up body. Never logs keys. Returns None on miss/fail."""
    details = _llm_api_details()
    if not details:
        return None
    api_key, base_url, model = details
    name = quote["prospect_name"] if not isinstance(quote, dict) else quote["prospect_name"]
    title = quote["title"] if not isinstance(quote, dict) else quote["title"]
    notes = quote["notes"] if not isinstance(quote, dict) else quote.get("notes")
    business = _env("BUSINESS_NAME") or "us"
    from_name = _env("FROM_NAME") or business
    prompt = (
        f"Write a short follow-up email body (no subject line) after a prospect opened a quote. "
        f"Business: {business}. From: {from_name}. Prospect: {name}. Quote title: {title}. "
        f"Owner notes (internal, optional tone only): {notes or 'none'}. "
        f"Reference that they opened the quote, offer to answer questions, one soft CTA. "
        f"Plain text only. No placeholders."
    )
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "You write concise professional follow-up emails."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.6,
            "max_tokens": 400,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"].strip()
        return text or None
    except Exception as exc:  # noqa: BLE001 — never log API keys
        app.logger.warning("LLM draft failed: %s", type(exc).__name__)
        return None

def create_followup_draft(conn: sqlite3.Connection, quote: sqlite3.Row) -> None:
    """Insert exactly one followups row if missing. Template first; optional LLM later."""
    existing = conn.execute(
        "SELECT id FROM followups WHERE quote_id = ?", (quote["id"],)
    ).fetchone()
    if existing is not None:
        return
    subject = followup_template_subject(quote)
    body = followup_template_body(quote)
    now = utc_now_iso()
    try:
        conn.execute(
            """
            INSERT INTO followups (quote_id, subject, body, status, created_at, sent_at)
            VALUES (?, ?, ?, 'queued', ?, NULL)
            """,
            (quote["id"], subject, body, now),
        )
        conn.execute(
            """
            UPDATE quotes SET status = 'followup_queued'
            WHERE id = ? AND status IN ('opened', 'link_ready', 'sent')
            """,
            (quote["id"],),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return

    # Optional LLM body replacement (sync ≤3s). Never blocks if already timed out conceptually.
    if llm_configured():
        llm_body = maybe_llm_followup_body(quote, timeout=3.0)
        if llm_body:
            conn.execute(
                "UPDATE followups SET body = ? WHERE quote_id = ? AND status = 'queued'",
                (llm_body, quote["id"]),
            )
            conn.commit()

def send_owner_open_alert(quote: sqlite3.Row) -> None:
    if not smtp_configured():
        return
    notify = _env("OWNER_NOTIFY_EMAIL")
    if not notify:
        return
    base = public_base_url() or "http://localhost:8080"
    subject = f"Quote opened: {quote['title']}"
    body = (
        f"Prospect: {quote['prospect_name']} <{quote['prospect_email']}>\n"
        f"Opened at: {quote['opened_at'] or utc_now_iso()}\n"
        f"Open count: {quote['open_count'] or 1}\n"
        f"\n"
        f"Open in OpenPing: {base}/quotes/{quote['id']}\n"
    )
    try:
        send_smtp(notify, subject, body)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("Owner alert failed for quote_id=%s: %s", quote["id"], type(exc).__name__)

def record_open_side_effects(quote_id: int, ip: str | None, ua: str | None) -> bool:
    """Idempotent first-open. Returns True if this call won the first-open race."""
    conn = connect_db()
    try:
        now = utc_now_iso()
        cur = conn.execute(
            """
            UPDATE quotes
            SET status = 'opened', opened_at = ?, open_count = open_count + 1
            WHERE id = ? AND status IN ('link_ready', 'sent')
            """,
            (now, quote_id),
        )
        won_first = cur.rowcount == 1
        if won_first:
            conn.execute(
                """
                INSERT INTO opens (quote_id, at, ip, user_agent, is_first)
                VALUES (?, ?, ?, ?, 1)
                """,
                (quote_id, now, ip, ua),
            )
            conn.commit()
            quote = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
            create_followup_draft(conn, quote)
            # Refresh after follow-up may have set followup_queued
            quote = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
            send_owner_open_alert(quote)
            return True

        # Subsequent open: log + increment count, no re-alert / no second follow-up
        conn.execute(
            "UPDATE quotes SET open_count = open_count + 1 WHERE id = ?",
            (quote_id,),
        )
        conn.execute(
            """
            INSERT INTO opens (quote_id, at, ip, user_agent, is_first)
            VALUES (?, ?, ?, ?, 0)
            """,
            (quote_id, now, ip, ua),
        )
        conn.commit()
        return False
    finally:
        conn.close()

def handle_open_beacon(quote: sqlite3.Row) -> None:
    ua = request.headers.get("User-Agent")
    if is_bot_ua(ua):
        return
    # For gif route, skip if Sec-Fetch-Dest is image-only prefetch? PRD: skip side effects
    # when Sec-Fetch-Dest is image only for the gif route — actually it says "or when
    # Sec-Fetch-Dest is image only for the gif route" as part of bot filter. Reading again:
    # "skip open-event side effects when User-Agent contains ... OR when Sec-Fetch-Dest is
    # image only for the gif route." That would skip ALL gif loads since gif is image dest.
    # That can't be right for the pixel purpose. Likely means: treat as bot when Dest is
    # something else? Re-read: "or when Sec-Fetch-Dest is image only for the gif route."
    # I think they meant skip for Dest that is NOT the intentional load — but the wording
    # is odd. Looking at sibling products and PRD §7: pixel has same first-open side effects.
    # I'll only apply bot UA filter for both routes; for gif, if Sec-Fetch-Dest is empty
    # or document we still fire. The "image only" phrase might mean: when checking bot
    # heuristics on gif, Dest=image alone is not enough to fire without a real UA — i.e.
    # don't treat Dest=image as a human signal. We already use UA. Proceed with UA only.
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if ip and "," in ip:
        ip = ip.split(",")[0].strip()
    record_open_side_effects(quote["id"], ip, ua)

# --- Routes ---

@app.get("/health")

def health():

    return jsonify(

        {

            "status": "ok",

            "smtp_configured": smtp_configured(),

            "llm_configured": llm_configured(),

        }

    )



@app.route("/login", methods=["GET", "POST"])

def login():

    nxt = _safe_next(request.values.get("next"))

    if not owner_password():

        return redirect(nxt)

    if session.get("owner"):

        return redirect(nxt)

    if request.method == "POST":

        provided = request.form.get("password") or ""

        if secrets_equal(provided, owner_password()):

            session["owner"] = True

            return redirect(nxt)

        flash("Incorrect password.", "error")

    return render_template("login.html", next=nxt, public=True)



@app.get("/logout")

def logout():

    session.clear()

    return redirect(url_for("login"))



@app.get("/")

def index():

    rows = get_db().execute(

        """

        SELECT q.*, f.status AS followup_status

        FROM quotes q

        LEFT JOIN followups f ON f.quote_id = q.id

        ORDER BY q.id DESC

        """

    ).fetchall()

    return render_template("index.html", quotes=rows)



@app.route("/quotes/new", methods=["GET", "POST"])

def new_quote():

    if request.method == "GET":

        return render_template("new_quote.html")



    title = (request.form.get("title") or "").strip()

    prospect_name = (request.form.get("prospect_name") or "").strip()

    prospect_email = (request.form.get("prospect_email") or "").strip()

    notes = (request.form.get("notes") or "").strip() or None

    upload = request.files.get("file")



    if not title or not prospect_name or not prospect_email:

        flash("Title, prospect name, and prospect email are required.", "error")

        return render_template("new_quote.html"), 400

    if not valid_email(prospect_email):

        flash("Prospect email looks invalid.", "error")

        return render_template("new_quote.html"), 400

    if upload is None or not upload.filename:

        flash("Upload one PDF or HTML file.", "error")

        return render_template("new_quote.html"), 400



    filename = secure_filename(upload.filename)

    ext = Path(filename).suffix.lower()

    kind = ALLOWED_EXT.get(ext)

    if not kind:

        flash("Only PDF or HTML uploads are allowed.", "error")

        return render_template("new_quote.html"), 400



    token = secrets.token_hex(32)

    stored_name = f"{token}{ext}"

    dest = upload_dir() / stored_name

    upload.save(dest)



    now = utc_now_iso()

    conn = get_db()

    cur = conn.execute(

        """

        INSERT INTO quotes (

            title, prospect_name, prospect_email, notes, token,

            file_path, file_kind, status, open_count, opened_at, link_sent_at, created_at

        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'link_ready', 0, NULL, NULL, ?)

        """,

        (title, prospect_name, prospect_email, notes, token, str(dest), kind, now),

    )

    conn.commit()

    quote_id = cur.lastrowid

    flash("Quote created. Copy the tracked link to share.", "ok")

    return redirect(url_for("quote_detail", quote_id=quote_id))



@app.get("/quotes/<int:quote_id>")

def quote_detail(quote_id: int):

    quote = get_quote(quote_id)

    if quote is None:

        abort(404)

    followup = get_followup(quote_id)

    opens = (

        get_db()

        .execute(

            "SELECT * FROM opens WHERE quote_id = ? ORDER BY id DESC LIMIT 50",

            (quote_id,),

        )

        .fetchall()

    )

    link = tracked_url(quote["token"])

    return render_template(

        "quote.html",

        quote=quote,

        followup=followup,

        opens=opens,

        tracked_link=link,

        suggested_subject=suggested_send_subject(quote),

        suggested_body=suggested_send_body(quote),

    )



@app.post("/quotes/<int:quote_id>/mark-sent")

def mark_sent(quote_id: int):

    quote = get_quote(quote_id)

    if quote is None:

        abort(404)

    if quote["status"] in ("link_ready", "draft"):

        get_db().execute(

            "UPDATE quotes SET status = 'sent', link_sent_at = ? WHERE id = ?",

            (utc_now_iso(), quote_id),

        )

        get_db().commit()

        flash("Marked as sent.", "ok")

    else:

        flash("Status unchanged.", "ok")

    return redirect(url_for("quote_detail", quote_id=quote_id))



@app.post("/quotes/<int:quote_id>/send-link")

def send_link(quote_id: int):

    quote = get_quote(quote_id)

    if quote is None:

        abort(404)

    if not smtp_configured():

        flash("SMTP is not configured.", "error")

        return redirect(url_for("quote_detail", quote_id=quote_id))

    if quote["link_sent_at"]:

        flash("Link email already sent once.", "error")

        return redirect(url_for("quote_detail", quote_id=quote_id))

    try:

        send_smtp(

            quote["prospect_email"],

            suggested_send_subject(quote),

            suggested_send_body(quote),

        )

        get_db().execute(

            """

            UPDATE quotes SET status = CASE

                WHEN status IN ('link_ready', 'draft') THEN 'sent'

                ELSE status

            END, link_sent_at = ? WHERE id = ?

            """,

            (utc_now_iso(), quote_id),

        )

        get_db().commit()

        flash("Link emailed to prospect.", "ok")

    except Exception as exc:  # noqa: BLE001

        app.logger.warning("send-link failed quote_id=%s: %s", quote_id, type(exc).__name__)

        flash("Could not send email. Check SMTP settings.", "error")

    return redirect(url_for("quote_detail", quote_id=quote_id))



@app.post("/quotes/<int:quote_id>/skip")

def skip_quote(quote_id: int):

    quote = get_quote(quote_id)

    if quote is None:

        abort(404)

    if quote["status"] not in ("followup_sent", "skipped"):

        get_db().execute(

            "UPDATE quotes SET status = 'skipped' WHERE id = ?",

            (quote_id,),

        )

        get_db().commit()

        flash("Quote skipped.", "ok")

    return redirect(url_for("quote_detail", quote_id=quote_id))



@app.post("/quotes/<int:quote_id>/followup")

def followup_action(quote_id: int):

    quote = get_quote(quote_id)

    if quote is None:

        abort(404)

    followup = get_followup(quote_id)

    if followup is None:

        flash("No follow-up draft yet.", "error")

        return redirect(url_for("quote_detail", quote_id=quote_id))



    action = (request.form.get("action") or "").strip()

    conn = get_db()



    if action == "save":

        subject = (request.form.get("subject") or "").strip()

        body = (request.form.get("body") or "").strip()

        if not subject or not body:

            flash("Subject and body are required.", "error")

            return redirect(url_for("quote_detail", quote_id=quote_id))

        if followup["status"] == "queued":

            conn.execute(

                "UPDATE followups SET subject = ?, body = ? WHERE id = ?",

                (subject, body, followup["id"]),

            )

            conn.commit()

            flash("Draft saved.", "ok")

        return redirect(url_for("quote_detail", quote_id=quote_id))



    if action == "approve_send":

        if followup["status"] == "sent" or quote["status"] == "followup_sent":

            flash("Follow-up already sent.", "ok")

            return redirect(url_for("quote_detail", quote_id=quote_id))

        if not smtp_configured():

            flash("SMTP is required to approve & send.", "error")

            return redirect(url_for("quote_detail", quote_id=quote_id))

        subject = (request.form.get("subject") or followup["subject"]).strip()

        body = (request.form.get("body") or followup["body"]).strip()

        try:

            send_smtp(quote["prospect_email"], subject, body)

            now = utc_now_iso()

            conn.execute(

                """

                UPDATE followups SET subject = ?, body = ?, status = 'sent', sent_at = ?

                WHERE id = ? AND status = 'queued'

                """,

                (subject, body, now, followup["id"]),

            )

            conn.execute(

                "UPDATE quotes SET status = 'followup_sent' WHERE id = ?",

                (quote_id,),

            )

            conn.commit()

            flash("Follow-up sent.", "ok")

        except Exception as exc:  # noqa: BLE001

            app.logger.warning("approve-send failed quote_id=%s: %s", quote_id, type(exc).__name__)

            flash("Could not send follow-up. Check SMTP settings.", "error")

        return redirect(url_for("quote_detail", quote_id=quote_id))



    if action == "mark_sent":

        if followup["status"] == "sent" or quote["status"] == "followup_sent":

            flash("Follow-up already marked sent.", "ok")

            return redirect(url_for("quote_detail", quote_id=quote_id))

        now = utc_now_iso()

        subject = (request.form.get("subject") or followup["subject"]).strip()

        body = (request.form.get("body") or followup["body"]).strip()

        conn.execute(

            """

            UPDATE followups SET subject = ?, body = ?, status = 'sent', sent_at = ?

            WHERE id = ? AND status IN ('queued', 'skipped')

            """,

            (subject, body, now, followup["id"]),

        )

        # Also allow from queued

        if conn.total_changes == 0:

            conn.execute(

                """

                UPDATE followups SET subject = ?, body = ?, status = 'sent', sent_at = ?

                WHERE id = ?

                """,

                (subject, body, now, followup["id"]),

            )

        conn.execute(

            "UPDATE quotes SET status = 'followup_sent' WHERE id = ?",

            (quote_id,),

        )

        conn.commit()

        flash("Follow-up marked sent.", "ok")

        return redirect(url_for("quote_detail", quote_id=quote_id))



    if action == "skip":

        if followup["status"] == "sent":

            flash("Follow-up already sent; cannot skip.", "error")

            return redirect(url_for("quote_detail", quote_id=quote_id))

        conn.execute(

            "UPDATE followups SET status = 'skipped' WHERE id = ? AND status = 'queued'",

            (followup["id"],),

        )

        conn.execute(

            "UPDATE quotes SET status = 'skipped' WHERE id = ? AND status != 'followup_sent'",

            (quote_id,),

        )

        conn.commit()

        flash("Follow-up skipped.", "ok")

        return redirect(url_for("quote_detail", quote_id=quote_id))



    flash("Unknown action.", "error")

    return redirect(url_for("quote_detail", quote_id=quote_id))



@app.get("/v/<token>")

def public_view(token: str):

    quote = get_quote_by_token(token)

    if quote is None:

        abort(404)

    handle_open_beacon(quote)



    path = Path(quote["file_path"])

    if not path.is_file():

        abort(404)



    if quote["file_kind"] == "pdf":

        data = path.read_bytes()

        return Response(

            data,

            mimetype="application/pdf",

            headers={

                "Content-Disposition": f'inline; filename="{path.name}"',

            },

        )



    # HTML: render inside minimal chrome

    try:

        html_body = path.read_text(encoding="utf-8", errors="replace")

    except OSError:

        abort(404)

    # Strip outer html/body if present for embedding — keep as-is in iframe-like chrome

    return render_template(

        "public_html.html",

        quote=quote,

        html_content=html_body,

        business_name=_env("BUSINESS_NAME"),

        public=True,

        pixel_url=url_for("tracking_pixel", token=token),

    )



@app.get("/t/<token>.gif")

def tracking_pixel(token: str):

    quote = get_quote_by_token(token)

    if quote is None:

        abort(404)

    handle_open_beacon(quote)

    return Response(PIXEL_GIF, mimetype="image/gif")



@app.errorhandler(404)

def not_found(_e):

    return render_template("404.html", public=True), 404

init_db()


if __name__ == "__main__":
    port = int(_env("PORT") or "8080")
    app.run(host="0.0.0.0", port=port, debug=False)
