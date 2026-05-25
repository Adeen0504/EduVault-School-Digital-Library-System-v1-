"""
╔══════════════════════════════════════════════════════════════════╗
║             EduVault — School Digital Library System             ║
║                         Backend  v1.1                            ║
║                                                                  ║
║  Stack  : Python 3.10+  |  Flask  |  SQLite  |  Werkzeug        ║
║  Storage: Gmail  (meeradeenali01@gmail.com)  via SMTP + IMAP     ║
║  Author : EduVault Team                                          ║
╚══════════════════════════════════════════════════════════════════╝

HOW FILE STORAGE WORKS:
  Every uploaded file is sent as an email attachment to your Gmail.
  Gmail stores it for free (15 GB total).
  When a student views a file, the app fetches it from Gmail via IMAP
  and streams it directly to the browser — no local disk needed.

PROJECT STRUCTURE:
  app.py          ← this file
  .env            ← Gmail app password (never commit this to git)
  eduvault.db     ← SQLite — stores metadata only, no files
  logs/
    eduvault.log  ← rotating application log (auto-created)
  templates/
    index.html
    student.html
    faculty-login.html
    faculty-register.html
    faculty-profile.html
    pdf-viewer.html

INSTALL & RUN:
  pip install flask werkzeug python-dotenv
  python app.py
  → Open http://localhost:5000

GMAIL SETUP (one-time):
  1. Go to myaccount.google.com → Security
  2. Turn ON 2-Step Verification
  3. Search "App Passwords" → Generate one for "EduVault"
  4. Copy the 16-char password into your .env file as GMAIL_APP_PASS
"""

# ── IMPORTS ───────────────────────────────────────────────────────────────────
import os
import sqlite3
import logging
import hashlib
import secrets
import uuid
import time
import smtplib
import imaplib
import email as email_lib
from email.mime.multipart  import MIMEMultipart
from email.mime.base       import MIMEBase
from email.mime.text       import MIMEText
from email                 import encoders
from datetime              import datetime, timedelta
from functools             import wraps
from logging.handlers      import RotatingFileHandler
from pathlib               import Path

from flask import (
    Flask, request, jsonify, send_from_directory,
    session, abort, g, Response,
)
from werkzeug.utils import secure_filename

# Load .env file if present (pip install python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass   # dotenv optional — env vars can be set manually too


# ╔══════════════════════════════════╗
# ║        APP CONFIG & PATHS        ║
# ╚══════════════════════════════════╝

BASE_DIR   = Path(__file__).resolve().parent
LOG_DIR    = BASE_DIR / "logs"
DB_PATH    = BASE_DIR / "eduvault.db"
STATIC_DIR = BASE_DIR / "templates"

LOG_DIR.mkdir(exist_ok=True)
# NOTE: No uploads/ folder — Gmail is our storage

ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "ppt", "pptx", "png", "jpg", "jpeg"}
MAX_FILE_MB        = 24           # Gmail attachment limit is 25 MB; keep 1 MB buffer
SESSION_LIFETIME   = 60 * 60 * 8  # 8-hour sessions

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="", template_folder=str(STATIC_DIR))
# ── SECRET KEY: stable across restarts ──────────────────────────────────────
# A new random key every restart invalidates all session cookies, logging
# everyone out and showing "Unauthorised" in the logs.
_secret_key_file = BASE_DIR / "secret.key"
if os.environ.get("EDUVAULT_SECRET"):
    app.secret_key = os.environ["EDUVAULT_SECRET"]
elif _secret_key_file.exists():
    app.secret_key = _secret_key_file.read_text().strip()
else:
    _new_key = secrets.token_hex(32)
    _secret_key_file.write_text(_new_key)
    app.secret_key = _new_key
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_MB * 1024 * 1024
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(seconds=SESSION_LIFETIME)


# ╔══════════════════════════════════╗
# ║        GMAIL CONFIGURATION       ║
# ╚══════════════════════════════════╝

GMAIL_ADDRESS  = os.environ.get("GMAIL_ADDRESS",  "meeradeenali01@gmail.com")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")   # set in .env — never hardcode
SMTP_HOST      = "smtp.gmail.com"
SMTP_PORT      = 587
IMAP_HOST      = "imap.gmail.com"

# Every EduVault email gets this prefix in the subject so we can
# search Gmail precisely without touching personal emails
GMAIL_TAG = "[EduVault]"

# Gmail label/folder where we store uploads (auto-created by Gmail)
GMAIL_LABEL = "EduVault-Library"


# ╔══════════════════════════════════╗
# ║          LOGGING SETUP           ║
# ╚══════════════════════════════════╝

def setup_logging() -> logging.Logger:
    """
    Rotating file logger  → logs/eduvault.log  (5 MB × 5 backups)
    Console logger        → INFO and above only
    Format: TIMESTAMP | LEVEL | MODULE | MESSAGE
    """
    log_file = LOG_DIR / "eduvault.log"
    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(module)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    console_handler.setLevel(logging.INFO)

    logger = logging.getLogger("eduvault")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


log = setup_logging()


# ╔══════════════════════════════════╗
# ║          DATABASE SETUP          ║
# ╚══════════════════════════════════╝

SCHEMA = """
-- Faculty accounts
CREATE TABLE IF NOT EXISTS faculty (
    id            TEXT PRIMARY KEY,     -- UUID
    name          TEXT NOT NULL,        -- e.g. "Mr. Sharma"
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,        -- SHA-256 hex
    subject       TEXT,                 -- primary subject they teach
    primary_class TEXT,                 -- primary class they teach
    joined_date   TEXT NOT NULL         -- ISO datetime string
);

-- File metadata  (actual file bytes live in Gmail)
CREATE TABLE IF NOT EXISTS files (
    id            TEXT PRIMARY KEY,     -- UUID (used in all API URLs)
    original_name TEXT NOT NULL,        -- filename the teacher uploaded
    gmail_msg_id  TEXT NOT NULL,        -- Gmail Message-ID header → used to fetch from IMAP
    title         TEXT,                 -- optional display title
    class_name    TEXT NOT NULL,        -- e.g. "Class 9"
    subject       TEXT NOT NULL,        -- e.g. "Science"
    size_mb       REAL NOT NULL,        -- file size in MB
    extension     TEXT NOT NULL,        -- pdf / docx / pptx …
    uploaded_by   TEXT NOT NULL,        -- faculty.id  (FK)
    uploaded_name TEXT NOT NULL,        -- faculty display name  (denormalised)
    upload_date   TEXT NOT NULL,        -- ISO datetime string
    FOREIGN KEY(uploaded_by) REFERENCES faculty(id)
);
"""


def get_db() -> sqlite3.Connection:
    """Return a per-request SQLite connection stored on Flask's `g`."""
    if "db" not in g:
        g.db = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist."""
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()
    log.info("Database initialised at %s", DB_PATH)


# ╔══════════════════════════════════════════════════════════════╗
# ║                  GMAIL HELPERS                               ║
# ╚══════════════════════════════════════════════════════════════╝

def _imap_connect() -> imaplib.IMAP4_SSL:
    """
    Open and return an authenticated IMAP connection to Gmail.
    Always call .logout() when done.
    """
    if not GMAIL_APP_PASS:
        raise RuntimeError(
            "GMAIL_APP_PASS is not set. "
            "Add it to your .env file. "
            "See the file header for setup instructions."
        )
    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
    return mail


def _smtp_send(msg: MIMEMultipart) -> None:
    """Send an email message via Gmail SMTP (TLS)."""
    if not GMAIL_APP_PASS:
        raise RuntimeError("GMAIL_APP_PASS is not set in .env")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
        server.send_message(msg)


def _get_gmail_message_id_by_token(upload_token: str) -> str | None:
    """
    After sending an upload email, connect via IMAP and return the
    Message-ID of the email whose subject contains the unique upload_token.
    Using a token instead of "latest email" prevents race conditions when
    two uploads happen simultaneously.
    """
    try:
        mail = _imap_connect()
        mail.select("inbox")

        # Search for the email that carries our unique upload token in the subject
        _, data = mail.search(None, f'(SUBJECT "{upload_token}")')
        ids = data[0].split()
        if not ids:
            mail.logout()
            return None

        # There should be exactly one match; take the last just in case
        latest_id = ids[-1]
        _, msg_data = mail.fetch(latest_id, "(RFC822.HEADER)")
        mail.logout()

        raw_header = msg_data[0][1]
        parsed     = email_lib.message_from_bytes(raw_header)
        return parsed.get("Message-ID", "").strip()

    except Exception as exc:
        log.error("IMAP Message-ID fetch error: %s", exc)
        return None


def _fetch_attachment_from_gmail(gmail_msg_id: str) -> tuple[bytes, str]:
    """
    Search Gmail inbox for the email whose Message-ID matches `gmail_msg_id`,
    extract the first attachment, and return (file_bytes, original_filename).

    Raises FileNotFoundError if the email is not found.
    Raises ValueError if the email has no attachment.
    """
    mail = _imap_connect()
    mail.select("inbox")

    # IMAP header search for exact Message-ID
    _, data = mail.search(None, f'(HEADER Message-ID "{gmail_msg_id}")')
    ids = data[0].split()

    if not ids:
        mail.logout()
        raise FileNotFoundError(
            f"No email found for Message-ID: {gmail_msg_id}. "
            "It may have been deleted from Gmail."
        )

    _, msg_data = mail.fetch(ids[0], "(RFC822)")
    mail.logout()

    raw_email = msg_data[0][1]
    parsed    = email_lib.message_from_bytes(raw_email)

    for part in parsed.walk():
        if part.get_content_disposition() == "attachment":
            filename   = part.get_filename() or "file"
            file_bytes = part.get_payload(decode=True)
            return file_bytes, filename

    raise ValueError(f"Email {gmail_msg_id} has no attachment.")


def _delete_gmail_email(gmail_msg_id: str) -> bool:
    """
    Find the email by Message-ID and move it to Gmail Trash.
    Returns True on success, False if not found.
    """
    try:
        mail = _imap_connect()
        mail.select("inbox")

        _, data = mail.search(None, f'(HEADER Message-ID "{gmail_msg_id}")')
        ids = data[0].split()
        if not ids:
            mail.logout()
            return False

        # Mark as deleted and expunge
        mail.store(ids[0], "+FLAGS", "\\Deleted")
        mail.expunge()
        mail.logout()
        log.info("Gmail email deleted for Message-ID: %s", gmail_msg_id)
        return True

    except Exception as exc:
        log.error("Gmail delete error: %s", exc)
        return False


# ╔══════════════════════════════════╗
# ║         UTILITY HELPERS          ║
# ╚══════════════════════════════════╝

def hash_password(plain: str) -> str:
    """SHA-256 hex digest of the password."""
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def allowed_file(filename: str) -> bool:
    """Return True if the file extension is allowed."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def faculty_required(f):
    """Decorator — returns 401 if no valid faculty session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "faculty_id" not in session:
            log.warning("Unauthorised access attempt to %s", request.path)
            return jsonify({"error": "Authentication required."}), 401
        return f(*args, **kwargs)
    return decorated


def current_faculty() -> dict | None:
    """Return the logged-in faculty row as a dict, or None."""
    fid = session.get("faculty_id")
    if not fid:
        return None
    row = get_db().execute("SELECT * FROM faculty WHERE id = ?", (fid,)).fetchone()
    return dict(row) if row else None


def format_date(dt: datetime) -> str:
    """'15 Jan 2025' — works on Windows and Linux."""
    return dt.strftime("%d %b %Y").lstrip("0")


def _file_row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a files DB row into a JSON-serialisable dict for the frontend."""
    return {
        "id":           row["id"],
        "name":         row["original_name"],
        "title":        row["title"] or row["original_name"],
        "class_name":   row["class_name"],
        "subject":      row["subject"],
        "size":         f"{row['size_mb']} MB",
        "extension":    row["extension"],
        "uploaded_by":  row["uploaded_name"],
        "upload_date":  row["upload_date"],
        "download_url": f"/api/files/{row['id']}/download",
    }


# ╔══════════════════════════════════╗
# ║       STATIC PAGE ROUTES         ║
# ╚══════════════════════════════════╝

@app.route("/")
def index():
    log.debug("Serving index.html")
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.route("/<path:filename>")
def static_pages(filename):
    safe = STATIC_DIR / filename
    if safe.exists():
        log.debug("Serving static: %s", filename)
        return send_from_directory(str(STATIC_DIR), filename)
    abort(404)


# ╔══════════════════════════════════════════════════════════════╗
# ║                  FACULTY AUTH  API                           ║
# ║  POST /api/faculty/register  → create account               ║
# ║  POST /api/faculty/login     → start session                ║
# ║  POST /api/faculty/logout    → end session                  ║
# ║  GET  /api/faculty/me        → current user info            ║
# ╚══════════════════════════════════════════════════════════════╝

@app.route("/api/faculty/register", methods=["POST"])
def faculty_register():
    """
    Register a new faculty account.

    Body (JSON):
    {
        "name"         : "Mr. Sharma",
        "email"        : "sharma@school.edu",
        "password"     : "secret123",
        "subject"      : "Science",
        "primary_class": "Class 9"
    }
    Returns 201 with { "faculty": { … } }
    """
    data      = request.get_json(silent=True) or {}
    name      = (data.get("name")          or "").strip()
    email     = (data.get("email")         or "").strip().lower()
    password  = (data.get("password")      or "").strip()
    subject   = (data.get("subject")       or "").strip()
    pclass    = (data.get("primary_class") or "").strip()

    # Validate
    errors = {}
    if not name:                      errors["name"]     = "Name is required."
    if not email or "@" not in email: errors["email"]    = "Valid email is required."
    if len(password) < 6:             errors["password"] = "Password must be at least 6 characters."
    # subject is optional — faculty can update it from their profile later
    if errors:
        log.warning("Register validation failed: %s", errors)
        return jsonify({"error": "Validation failed", "fields": errors}), 422

    db = get_db()
    if db.execute("SELECT id FROM faculty WHERE email = ?", (email,)).fetchone():
        log.warning("Register: duplicate email %s", email)
        return jsonify({"error": "This email is already registered."}), 409

    fid         = str(uuid.uuid4())
    joined_date = datetime.utcnow().isoformat()

    db.execute(
        """INSERT INTO faculty
           (id, name, email, password_hash, subject, primary_class, joined_date)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (fid, name, email, hash_password(password), subject, pclass, joined_date),
    )
    db.commit()

    session.permanent   = True
    session["faculty_id"] = fid

    log.info("Faculty registered: %s <%s> id=%s", name, email, fid)
    return jsonify({"faculty": {
        "id": fid, "name": name, "email": email,
        "subject": subject, "primary_class": pclass,
        "joined_date": format_date(datetime.utcnow()),
    }}), 201


@app.route("/api/faculty/login", methods=["POST"])
def faculty_login():
    """
    Authenticate a faculty member.

    Body: { "email": "…", "password": "…" }
    Returns 200 with { "faculty": { … } }
    """
    data     = request.get_json(silent=True) or {}
    email    = (data.get("email")    or "").strip().lower()
    password = (data.get("password") or "").strip()

    if not email or not password:
        return jsonify({"error": "Email and password are required."}), 400

    db  = get_db()
    row = db.execute("SELECT * FROM faculty WHERE email = ?", (email,)).fetchone()

    if not row or row["password_hash"] != hash_password(password):
        log.warning("Login failed: %s", email)
        return jsonify({"error": "Invalid email or password."}), 401

    session.permanent   = True
    session["faculty_id"] = row["id"]

    log.info("Faculty logged in: %s <%s>", row["name"], email)
    return jsonify({"faculty": {
        "id":            row["id"],
        "name":          row["name"],
        "email":         row["email"],
        "subject":       row["subject"],
        "primary_class": row["primary_class"],
        "joined_date":   row["joined_date"],
    }}), 200


@app.route("/api/faculty/logout", methods=["POST"])
@faculty_required
def faculty_logout():
    fac = current_faculty()
    session.clear()
    log.info("Faculty logged out: %s", fac["email"] if fac else "unknown")
    return jsonify({"message": "Logged out successfully."}), 200


@app.route("/api/faculty/me", methods=["GET"])
@faculty_required
def faculty_me():
    fac = current_faculty()
    if not fac:
        return jsonify({"error": "Session expired."}), 401

    count = get_db().execute(
        "SELECT COUNT(*) FROM files WHERE uploaded_by = ?", (fac["id"],)
    ).fetchone()[0]

    return jsonify({"faculty": {
        "id":            fac["id"],
        "name":          fac["name"],
        "email":         fac["email"],
        "subject":       fac["subject"],
        "primary_class": fac["primary_class"],
        "joined_date":   fac["joined_date"],
        "upload_count":  count,
    }}), 200


# ╔══════════════════════════════════════════════════════════════╗
# ║                    FILE  API                                 ║
# ║                                                              ║
# ║  POST   /api/files/upload          Upload → saved to Gmail  ║
# ║  GET    /api/files                 List all (student view)  ║
# ║  GET    /api/files?class=X&sub=Y   Filtered list            ║
# ║  GET    /api/files/my              Faculty's own uploads    ║
# ║  GET    /api/files/<id>/download   Stream from Gmail        ║
# ║  DELETE /api/files/<id>            Delete from Gmail + DB   ║
# ╚══════════════════════════════════════════════════════════════╝

@app.route("/api/files/upload", methods=["POST"])
@faculty_required
def upload_file():
    """
    Receive a file from the faculty upload form,
    send it as an email attachment to meeradeenali01@gmail.com,
    and save the metadata (including Gmail Message-ID) to SQLite.

    Form fields:
      file        : the file binary (multipart)
      class_name  : e.g. "Class 9"
      subject     : e.g. "Science"
      title       : optional display title
    """
    fac        = current_faculty()
    class_name = (request.form.get("class_name") or "").strip()
    subject    = (request.form.get("subject")    or "").strip()
    title      = (request.form.get("title")      or "").strip()

    if not class_name:
        return jsonify({"error": "class_name is required."}), 400
    if not subject:
        return jsonify({"error": "subject is required."}), 400
    if "file" not in request.files:
        return jsonify({"error": "No file attached."}), 400

    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "Empty file."}), 400
    if not allowed_file(f.filename):
        log.warning("Blocked upload: %s by %s", f.filename, fac["email"])
        return jsonify({
            "error": f"File type not allowed. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        }), 415

    # Read file into memory
    file_bytes    = f.read()
    original_name = f.filename
    ext           = original_name.rsplit(".", 1)[1].lower()
    size_mb       = round(len(file_bytes) / (1024 * 1024), 2)

    # ── Build email ────────────────────────────────────────────
    #  Generate a unique token per upload so IMAP search is exact (no race condition)
    upload_token   = str(uuid.uuid4())
    msg            = MIMEMultipart()
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = GMAIL_ADDRESS
    msg["Subject"] = (
        f"{GMAIL_TAG} {class_name} | {subject} | "
        f"{title or original_name} | {fac['name']} | {upload_token}"
    )

    # ── Email body carries metadata as plain text ──────────────
    # This lets us recover info even without the database
    body = "\n".join([
        "=== EduVault File Upload ===",
        f"FILE_ID       : (assigned after save)",
        f"CLASS         : {class_name}",
        f"SUBJECT       : {subject}",
        f"TITLE         : {title or original_name}",
        f"ORIGINAL_NAME : {original_name}",
        f"EXTENSION     : {ext}",
        f"SIZE_MB       : {size_mb}",
        f"UPLOADED_BY   : {fac['email']}",
        f"UPLOADED_NAME : {fac['name']}",
        f"UPLOAD_DATE   : {datetime.utcnow().isoformat()}",
        "============================",
        "",
        "This email is auto-generated by EduVault.",
        "Do not delete unless you want to remove this file from the library.",
    ])
    msg.attach(MIMEText(body, "plain"))

    # ── Attach the actual file ─────────────────────────────────
    part = MIMEBase("application", "octet-stream")
    part.set_payload(file_bytes)
    encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition",
        f'attachment; filename="{original_name}"',
    )
    msg.attach(part)

    # ── Send via Gmail SMTP ────────────────────────────────────
    try:
        _smtp_send(msg)
        log.info(
            "File emailed to Gmail: '%s' | %s | %s | by %s",
            original_name, class_name, subject, fac["email"],
        )
    except Exception as exc:
        log.error("Gmail SMTP send failed: %s", exc)
        return jsonify({"error": "Failed to store file. Check Gmail credentials in .env"}), 500

    # ── Fetch the Message-ID of the email we just sent ─────────
    # Wait a moment for Gmail to process it, then search by unique token
    time.sleep(2)

    gmail_msg_id = _get_gmail_message_id_by_token(upload_token)
    if not gmail_msg_id:
        log.error("Could not retrieve Gmail Message-ID after upload")
        return jsonify({"error": "File sent but could not confirm storage. Try again."}), 500

    # ── Save metadata in SQLite ────────────────────────────────
    fid         = str(uuid.uuid4())
    upload_date = datetime.utcnow().isoformat()
    db          = get_db()
    db.execute(
        """INSERT INTO files
           (id, original_name, gmail_msg_id, title, class_name, subject,
            size_mb, extension, uploaded_by, uploaded_name, upload_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            fid, original_name, gmail_msg_id,
            title or original_name,
            class_name, subject, size_mb, ext,
            fac["id"], fac["name"], upload_date,
        ),
    )
    db.commit()

    log.info("File metadata saved: id=%s gmail_msg_id=%s", fid, gmail_msg_id)

    return jsonify({"file": {
        "id":           fid,
        "title":        title or original_name,
        "class_name":   class_name,
        "subject":      subject,
        "size_mb":      size_mb,
        "extension":    ext,
        "uploaded_by":  fac["name"],
        "upload_date":  format_date(datetime.utcnow()),
        "download_url": f"/api/files/{fid}/download",
    }}), 201


@app.route("/api/files", methods=["GET"])
def list_files():
    """
    List all library files.  No login required — students use this.

    Query params (both optional):
      class_name : filter by class   e.g. ?class_name=Class+9
      subject    : filter by subject e.g. ?subject=Science

    Returns 200 with { "files": [ … ] }
    """
    class_name = (request.args.get("class_name") or "").strip()
    subject    = (request.args.get("subject")    or "").strip()

    query  = "SELECT * FROM files WHERE 1=1"
    params = []
    if class_name:
        query += " AND class_name = ?"
        params.append(class_name)
    if subject:
        query += " AND subject = ?"
        params.append(subject)
    query += " ORDER BY upload_date DESC"

    rows  = get_db().execute(query, params).fetchall()
    files = [_file_row_to_dict(r) for r in rows]

    log.debug(
        "list_files: class='%s' subject='%s' → %d results",
        class_name, subject, len(files),
    )
    return jsonify({"files": files}), 200


@app.route("/api/files/my", methods=["GET"])
@faculty_required
def list_my_files():
    """
    List files uploaded by the currently logged-in faculty only.
    Supports same class_name / subject filters as /api/files.
    """
    fac        = current_faculty()
    class_name = (request.args.get("class_name") or "").strip()
    subject    = (request.args.get("subject")    or "").strip()

    query  = "SELECT * FROM files WHERE uploaded_by = ?"
    params = [fac["id"]]
    if class_name:
        query += " AND class_name = ?"
        params.append(class_name)
    if subject:
        query += " AND subject = ?"
        params.append(subject)
    query += " ORDER BY upload_date DESC"

    rows = get_db().execute(query, params).fetchall()
    return jsonify({"files": [_file_row_to_dict(r) for r in rows]}), 200


@app.route("/api/files/<file_id>/download", methods=["GET"])
def download_file(file_id):
    """
    Stream a file to the browser.
    No login required — students use this to view files in pdf-viewer.html.

    Fetches the actual file bytes from Gmail via IMAP using the stored
    Message-ID, then streams them directly to the browser.
    """
    db  = get_db()
    row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()

    if not row:
        log.warning("Download: file id not found in DB: %s", file_id)
        abort(404)

    log.info(
        "Fetching from Gmail: '%s' (id=%s) | %s | %s",
        row["original_name"], file_id, row["class_name"], row["subject"],
    )

    try:
        file_bytes, filename = _fetch_attachment_from_gmail(row["gmail_msg_id"])
    except FileNotFoundError as exc:
        log.error("Gmail file not found: %s", exc)
        return jsonify({
            "error": "File not found in Gmail. It may have been deleted."
        }), 404
    except Exception as exc:
        log.error("Gmail IMAP fetch error for id=%s: %s", file_id, exc)
        return jsonify({"error": "Could not retrieve file. Try again shortly."}), 500

    # Determine MIME type for inline viewing in the browser
    mime_map = {
        "pdf":  "application/pdf",
        "ppt":  "application/vnd.ms-powerpoint",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "doc":  "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "png":  "image/png",
        "jpg":  "image/jpeg",
        "jpeg": "image/jpeg",
    }
    mime = mime_map.get(row["extension"], "application/octet-stream")

    log.info(
        "Streaming '%s' (%s) to client | %d bytes",
        filename, mime, len(file_bytes),
    )

    return Response(
        file_bytes,
        mimetype=mime,
        headers={
            # 'inline' tells the browser to display it, not download it
            "Content-Disposition": f'inline; filename="{row["original_name"]}"',
            "Content-Length":      str(len(file_bytes)),
        },
    )


@app.route("/api/files/<file_id>", methods=["DELETE"])
@faculty_required
def delete_file(file_id):
    """
    Delete a file.
    - Removes the email from Gmail (moves to Trash)
    - Removes the metadata row from SQLite
    - Only the faculty member who uploaded it can delete it
    """
    fac = current_faculty()
    db  = get_db()
    row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()

    if not row:
        return jsonify({"error": "File not found."}), 404

    if row["uploaded_by"] != fac["id"]:
        log.warning(
            "Delete unauthorised: %s tried to delete file %s owned by %s",
            fac["email"], file_id, row["uploaded_by"],
        )
        return jsonify({"error": "You can only delete your own files."}), 403

    # Delete from Gmail
    deleted_from_gmail = _delete_gmail_email(row["gmail_msg_id"])
    if not deleted_from_gmail:
        log.warning(
            "Could not delete Gmail email for file %s (msg_id=%s) — "
            "may have been manually deleted. Proceeding with DB removal.",
            file_id, row["gmail_msg_id"],
        )

    # Remove from DB
    db.execute("DELETE FROM files WHERE id = ?", (file_id,))
    db.commit()

    log.info(
        "File deleted: '%s' (id=%s) by %s | Gmail removed: %s",
        row["original_name"], file_id, fac["email"], deleted_from_gmail,
    )
    return jsonify({"message": "File deleted successfully."}), 200


# ╔══════════════════════════════════════════════════════════════╗
# ║                    STATS  API                                ║
# ║  GET /api/stats  →  library statistics  (no login)          ║
# ╚══════════════════════════════════════════════════════════════╝

@app.route("/api/stats", methods=["GET"])
def stats():
    """Return aggregate counts for the faculty dashboard."""
    db = get_db()
    return jsonify({
        "total_files":    db.execute("SELECT COUNT(*) FROM files").fetchone()[0],
        "total_faculty":  db.execute("SELECT COUNT(*) FROM faculty").fetchone()[0],
        "total_classes":  db.execute("SELECT COUNT(DISTINCT class_name) FROM files").fetchone()[0],
        "total_subjects": db.execute("SELECT COUNT(DISTINCT subject) FROM files").fetchone()[0],
    }), 200


# ╔══════════════════════════════════╗
# ║         ERROR HANDLERS           ║
# ╚══════════════════════════════════╝

@app.errorhandler(404)
def not_found(e):
    log.warning("404 Not Found: %s %s", request.method, request.path)
    return jsonify({"error": "Resource not found."}), 404


@app.errorhandler(413)
def file_too_large(e):
    log.warning("413 File Too Large: %s", request.path)
    return jsonify({"error": f"File exceeds the {MAX_FILE_MB} MB limit."}), 413


@app.errorhandler(500)
def internal_error(e):
    log.exception("500 Internal Server Error: %s", e)
    return jsonify({"error": "Internal server error. Check eduvault.log for details."}), 500


# ╔══════════════════════════════════╗
# ║      REQUEST LIFECYCLE HOOKS     ║
# ╚══════════════════════════════════╝

@app.before_request
def log_request():
    log.debug("--> %s %s | IP: %s", request.method, request.path, request.remote_addr)


@app.after_request
def log_response(response):
    log.debug("<-- %s %s | %s", request.method, request.path, response.status_code)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"]        = "SAMEORIGIN"
    return response


# ╔══════════════════════════════════╗
# ║           ENTRY POINT            ║
# ╚══════════════════════════════════╝

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  EduVault School Digital Library  v1.1")
    log.info("=" * 60)
    log.info("  Gmail account : %s", GMAIL_ADDRESS)
    log.info("  App password  : %s", "SET ✓" if GMAIL_APP_PASS else "NOT SET ✗ — add to .env!")
    log.info("  Database      : %s", DB_PATH)
    log.info("  Log file      : %s", LOG_DIR / "eduvault.log")
    log.info("  Templates     : %s", STATIC_DIR)
    log.info("  Max upload    : %d MB (Gmail limit)", MAX_FILE_MB)
    log.info("=" * 60)

    if not GMAIL_APP_PASS:
        log.warning("⚠  GMAIL_APP_PASS is not set!")
        log.warning("   File uploads and downloads will NOT work.")
        log.warning("   See the file header for Gmail App Password setup instructions.")

    init_db()

    app.run(
        host  = "0.0.0.0",
        port  = int(os.environ.get("PORT", 5000)),
        debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true",
    )
