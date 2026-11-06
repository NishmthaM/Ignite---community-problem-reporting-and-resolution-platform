"""CivicTrack: community issue-reporting API + website in one Flask file.

  python civictrack.py                                   run the server
  python civictrack.py create-staff NAME EMAIL PASSWORD  create/promote a staff account
  gunicorn -w 1 --threads 8 civictrack:app               production
"""
import logging, os, re, secrets, sqlite3, sys, threading, time
from collections import defaultdict
from functools import wraps
from pathlib import Path

import bcrypt, jwt
from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

# ---------- config ----------
ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
env = os.environ.get
PROD = (env("NODE_ENV") or env("APP_ENV")) == "production"
HOST, PORT = env("HOST", "127.0.0.1"), int(env("PORT") or 3000)
SECRET = env("JWT_SECRET", "")
DB_FILE = (ROOT / env("DB_FILE", "./data/civictrack.sqlite")).resolve()
UPLOAD_DIR = (ROOT / env("UPLOAD_DIR", "./uploads")).resolve()
FRONTEND = env("FRONTEND_ORIGIN", "").strip()
TRUST_PROXY = env("TRUST_PROXY", "").strip()
MAX_PHOTO = 5 * 1024 * 1024
_m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", env("JWT_EXPIRES_IN", "8h"))
TOKEN_TTL = int(_m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[_m[2]] if _m else 8 * 3600

if len(SECRET) < 32 or SECRET.lower().startswith("replace-with"):
    sys.exit('Configuration error: set JWT_SECRET in .env to a random 32+ character value, e.g. '
             'python -c "import secrets; print(secrets.token_hex(48))"')

CATEGORIES = ["Roads and potholes", "Streetlights", "Waste and overflowing bins",
              "Drainage and flooding", "Parks and public spaces", "Water or sewer", "Other"]
STATUSES = ["Submitted", "Under review", "Accepted", "Assigned", "In progress", "Resolved", "Closed", "Rejected"]
PRIORITIES = ["High", "Medium", "Low"]
DEPARTMENTS = ["Unassigned", "Roads team", "Electrical services", "Sanitation", "Drainage",
               "Parks team", "Water services"]
HIDDEN = ("Submitted", "Rejected")  # never in the public listing
CLOSED = ("Resolved", "Closed", "Rejected")

# ---------- database ----------
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
_sql = lambda vals: ",".join(f"'{v}'" for v in vals)
_ts = f"TEXT NOT NULL DEFAULT ({NOW})"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'resident' CHECK (role IN ('resident','staff')),
  created_at {_ts});
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, reference TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id), category TEXT NOT NULL, title TEXT NOT NULL,
  description TEXT NOT NULL, location TEXT, latitude REAL, longitude REAL, contact TEXT,
  status TEXT NOT NULL DEFAULT 'Submitted' CHECK (status IN ({_sql(STATUSES)})),
  priority TEXT NOT NULL DEFAULT 'Medium' CHECK (priority IN ({_sql(PRIORITIES)})),
  department TEXT NOT NULL DEFAULT 'Unassigned', assignee TEXT, photo_file TEXT, duplicate_of TEXT,
  published INTEGER NOT NULL DEFAULT 0, created_at {_ts}, updated_at {_ts});
CREATE TABLE IF NOT EXISTS report_updates (
  id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  status TEXT NOT NULL, note TEXT NOT NULL, author_id INTEGER REFERENCES users(id), created_at {_ts});
CREATE TABLE IF NOT EXISTS internal_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  note TEXT NOT NULL, author_id INTEGER REFERENCES users(id), created_at {_ts});
CREATE TABLE IF NOT EXISTS support_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, contact TEXT NOT NULL,
  topic TEXT NOT NULL, message TEXT NOT NULL, created_at {_ts});
CREATE INDEX IF NOT EXISTS idx_reports_user ON reports(user_id);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
CREATE INDEX IF NOT EXISTS idx_reports_category ON reports(category);
CREATE INDEX IF NOT EXISTS idx_reports_published ON reports(published, updated_at);
CREATE INDEX IF NOT EXISTS idx_updates_report ON report_updates(report_id);
CREATE INDEX IF NOT EXISTS idx_notes_report ON internal_notes(report_id);
"""


def connect():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def db():  # per-request connection, closed on teardown
    if "db" not in g:
        g.db = connect()
    return g.db


UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
with connect() as _c:
    _c.execute("PRAGMA journal_mode = WAL")
    _c.executescript(SCHEMA)


# ---------- errors ----------
class ApiError(Exception):
    def __init__(self, status, message, headers=None):
        super().__init__(message)
        self.status, self.headers = status, headers or {}


def fail(status, message):
    raise ApiError(status, message)


def _json_error(status, message, headers=None):
    resp = jsonify({"error": message})
    resp.status_code = status
    resp.headers.update(headers or {})
    return resp


# ---------- validation ----------
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
REFERENCE_RE = re.compile(r"^CT-\d{6}$")


def json_body():
    if len(request.get_data(cache=True)) > 100 * 1024:
        fail(413, "Request body is too large.")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        fail(400, "Request body must be a JSON object.")
    return data


def text(value, label, lo, hi, required=True):
    """Trimmed, length-checked string; None for an absent optional field."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            fail(400, f"{label} is required.")
        return None
    if not isinstance(value, str):
        fail(400, f"{label} must be text.")
    value = value.replace("\x00", "").strip()
    if not lo <= len(value) <= hi:
        fail(400, f"{label} must be between {lo} and {hi} characters.")
    return value


def choice(value, label, allowed):
    if value not in allowed:
        fail(400, f"{label} must be one of: {', '.join(allowed)}.")
    return value


def number(value, label, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(400, f"{label} must be a number.")
    if not lo <= value <= hi:
        fail(400, f"{label} must be between {lo:g} and {hi:g}.")
    return float(value)


def int_arg(name, default, lo, hi):
    raw = request.args.get(name)
    if not raw:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        fail(400, f"{name} must be a whole number.")


def filters(specs, search_cols):
    """WHERE parts for query-string filters. specs: (arg, label, allowed, column);
    each value may be a comma-separated list. Plus a contains-search on `q`."""
    where, params = [], []
    for arg, label, allowed, column in specs:
        wanted = [s.strip() for s in request.args.get(arg, "").split(",") if s.strip()]
        for item in wanted:
            choice(item, label, allowed)
        if wanted:
            where.append(f"{column} IN ({','.join('?' * len(wanted))})")
            params += wanted
    q = request.args.get("q", "").strip()[:100]
    if q:
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in search_cols) + ")")
        params += [pattern] * len(search_cols)
    return where or ["1=1"], params


def page(where, params, order, view):
    """Run a filtered, paginated report query and return the JSON list response."""
    clause, limit = " AND ".join(where), int_arg("limit", 20, 1, 100)
    offset = int_arg("offset", 0, 0, 10**9)
    total = db().execute(f"SELECT COUNT(*) FROM reports r WHERE {clause}", params).fetchone()[0]
    rows = db().execute(f"SELECT r.* FROM reports r WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                        params + [limit, offset]).fetchall()
    return jsonify({"total": total, "items": [view(r) for r in rows]})


# ---------- security: passwords, sessions, rate limits ----------
def hash_password(password):  # bcrypt only reads the first 72 bytes
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt(12)).decode()


def verify_password(password, stored):
    try:
        return bcrypt.checkpw(password.encode()[:72], stored.encode())
    except ValueError:
        return False


_DUMMY_HASH = hash_password("not-a-real-password")  # equalises timing for unknown emails
_hits, _lock = defaultdict(list), threading.Lock()


def rate_check(key, limit, window):
    now = time.monotonic()
    with _lock:
        hits = _hits[key] = [t for t in _hits[key] if t > now - window]
        if len(hits) >= limit:
            raise ApiError(429, "Too many requests. Please wait a while and try again.",
                           {"Retry-After": str(int(hits[0] + window - now) + 1)})
        hits.append(now)
        if len(_hits) > 20000:  # keep memory bounded
            for k in [k for k, v in _hits.items() if not v]:
                del _hits[k]


def rate_limit(name, limit, window):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            rate_check(f"{name}:{request.remote_addr}", limit, window)
            return fn(*a, **kw)
        return wrapper
    return deco


def issue_token(user_id):
    now = int(time.time())
    return jwt.encode({"sub": str(user_id), "iat": now, "exp": now + TOKEN_TTL}, SECRET, algorithm="HS256")


def login_required(role=None):
    """Require a valid session (and `role` if given). The role is read from the DB, not the token."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                fail(401, "Please sign in to continue.")
            try:
                uid = int(jwt.decode(header[7:].strip(), SECRET, algorithms=["HS256"])["sub"])
            except (jwt.PyJWTError, KeyError, ValueError):
                fail(401, "Your session has expired. Please sign in again.")
            g.user = db().execute("SELECT id, name, email, role FROM users WHERE id = ?", (uid,)).fetchone()
            if g.user is None:
                fail(401, "Your session has expired. Please sign in again.")
            if role and g.user["role"] != role:
                fail(403, "Municipal staff access is required.")
            return fn(*a, **kw)
        return wrapper
    return deco


auth_required, staff_required = login_required(), login_required("staff")
public_user = lambda u: {k: u[k] for k in ("id", "name", "email", "role")}

# ---------- app: headers, CORS, errors ----------
app = Flask(__name__, static_folder=str(ROOT / "public"), static_url_path="")
app.json.sort_keys = False
app.config["MAX_CONTENT_LENGTH"] = MAX_PHOTO + 64 * 1024
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600 if PROD else 0
if TRUST_PROXY:
    n = int(TRUST_PROXY) if TRUST_PROXY.isdigit() else 1
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n)


@app.teardown_appcontext
def _close_db(_exc):
    conn = g.pop("db", None)
    if conn:
        conn.close()


CSP = "; ".join([
    "default-src 'self'", "base-uri 'self'", "font-src 'self' https: data:", "form-action 'self'",
    "frame-ancestors 'self'", "img-src 'self' data: blob:", "object-src 'none'", "script-src 'self'",
    "script-src-attr 'none'", "style-src 'self' https: 'unsafe-inline'",
    f"connect-src 'self' {FRONTEND}".strip(),
] + (["upgrade-insecure-requests"] if PROD else []))


def _cors(resp):
    if FRONTEND and request.headers.get("Origin") == FRONTEND:
        resp.headers.update({"Access-Control-Allow-Origin": FRONTEND,
                             "Access-Control-Allow-Headers": "Authorization, Content-Type",
                             "Access-Control-Allow-Methods": "GET, POST, PATCH, OPTIONS"})
        resp.headers.add("Vary", "Origin")


@app.before_request
def _before():
    if request.method == "OPTIONS" and FRONTEND:
        return app.make_response(("", 204))
    if request.path.startswith("/api/"):
        rate_check(f"api:{request.remote_addr}", 600, 15 * 60)


@app.after_request
def _after(resp):
    _cors(resp)
    resp.headers.update({
        "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN", "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "cross-origin" if FRONTEND else "same-origin"})
    if PROD:
        resp.headers["Strict-Transport-Security"] = "max-age=15552000; includeSubDomains"
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.errorhandler(ApiError)
def _api_error(e):
    return _json_error(e.status, str(e), e.headers)


@app.errorhandler(HTTPException)
def _http_error(e):
    if not request.path.startswith("/api"):
        return e
    msg = {404: "Not found.", 405: "Method not allowed.", 413: "File too large (maximum 5 MB)."}
    return _json_error(e.code or 500, msg.get(e.code, e.description or "Request failed."))


@app.errorhandler(Exception)
def _unexpected(e):
    app.logger.exception("Unhandled error: %s", e)
    return _json_error(500, "Something went wrong on our side. Please try again.")


# ---------- report helpers and views ----------
def report_by(column, value):
    return db().execute(f"SELECT * FROM reports WHERE {column} = ?", (value,)).fetchone()


def report_or_404(report_id):
    return report_by("id", report_id) or fail(404, "Report not found.")


def own_or_staff(report):
    if g.user["role"] != "staff" and report["user_id"] != g.user["id"]:
        fail(403, "You do not have access to this report.")
    return report


def pick(row, cols):  # snake_case columns -> camelCase keys
    return {re.sub(r"_(\w)", lambda m: m[1].upper(), c): row[c] for c in cols.split()}


def updates_for(report_id):
    rows = db().execute("SELECT status, note, created_at FROM report_updates WHERE report_id = ? ORDER BY id",
                        (report_id,))
    return [pick(r, "status note created_at") for r in rows]


def public_view(r):
    item = pick(r, "reference title category description location status created_at updated_at")
    if r["latitude"] is not None and r["longitude"] is not None:  # ~1 km precision for public data
        item["area"] = {"lat": round(r["latitude"], 2), "lng": round(r["longitude"], 2)}
    return item


def track_view(r):
    return {**pick(r, "reference title category status created_at updated_at"), "updates": updates_for(r["id"])}


def owner_view(r, updates=True):
    item = pick(r, "id reference category title description location latitude longitude contact "
                   "status department created_at updated_at")
    item["hasPhoto"] = bool(r["photo_file"])
    if updates:
        item["updates"] = updates_for(r["id"])
    return item


def staff_view(r, detail=False):
    who = db().execute("SELECT name, email FROM users WHERE id = ?", (r["user_id"],)).fetchone()
    item = {**owner_view(r, updates=detail), **pick(r, "priority assignee duplicate_of"),
            "reporter": dict(who) if who else None}
    if detail:
        notes = db().execute("""SELECT n.note, n.created_at, u.name AS author FROM internal_notes n
                                LEFT JOIN users u ON u.id = n.author_id WHERE n.report_id = ? ORDER BY n.id""",
                             (r["id"],))
        item["internalNotes"] = [pick(n, "note author created_at") for n in notes]
        related = db().execute("""SELECT reference, title, status FROM reports
                                  WHERE id != ? AND (reference = ? OR duplicate_of = ?)""",
                               (r["id"], r["duplicate_of"] or "", r["reference"]))
        item["related"] = [{**dict(x), "relation": "duplicateOf" if x["reference"] == r["duplicate_of"]
                            else "duplicatedBy"} for x in related]
    return item


def add_note(table, report_id, note, *extra):
    cols, marks = ("status, ", "?, ") if table == "report_updates" else ("", "")
    db().execute(f"INSERT INTO {table} (report_id, {cols}note, author_id) VALUES (?, {marks}?, ?)",
                 (report_id, *extra, note, g.user["id"]))


def created(payload):
    resp = jsonify(payload)
    resp.status_code = 201
    return resp


# ---------- general + auth + support ----------
@app.get("/")
def index():
    return send_from_directory(ROOT / "public", "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/meta")
def meta():
    return jsonify({"categories": CATEGORIES, "statuses": STATUSES,
                    "priorities": PRIORITIES, "departments": DEPARTMENTS})


@app.post("/api/auth/register")
@rate_limit("register", 20, 900)
def register():
    body = json_body()
    name = text(body.get("name"), "Name", 1, 100)
    email = text(body.get("email"), "Email", 3, 254).lower()
    if not EMAIL_RE.match(email):
        fail(400, "Please enter a valid email address.")
    password = body.get("password")
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        fail(400, "Password must be between 8 and 128 characters.")
    try:
        cur = db().execute("INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                           (name, email, hash_password(password)))
        db().commit()
    except sqlite3.IntegrityError:
        fail(409, "An account with this email already exists.")
    user = {"id": cur.lastrowid, "name": name, "email": email, "role": "resident"}
    return created({"token": issue_token(cur.lastrowid), "user": user})


@app.post("/api/auth/login")
@rate_limit("login", 20, 900)
def login():
    body = json_body()
    email, password = body.get("email"), body.get("password")
    if not (isinstance(email, str) and isinstance(password, str) and email and password):
        fail(400, "Email and password are required.")
    row = db().execute("SELECT id, name, email, role, password_hash FROM users WHERE email = ?",
                       (email.strip().lower(),)).fetchone()
    ok = verify_password(password, row["password_hash"] if row else _DUMMY_HASH)
    if row is None or not ok:
        fail(401, "Incorrect email or password.")
    return jsonify({"token": issue_token(row["id"]), "user": public_user(row)})


@app.get("/api/auth/me")
@auth_required
def me():
    return jsonify({"user": public_user(g.user)})


@app.post("/api/support")
@rate_limit("support", 5, 3600)
def support():
    body = json_body()
    db().execute("INSERT INTO support_messages (name, contact, topic, message) VALUES (?, ?, ?, ?)", (
        text(body.get("name"), "Name", 1, 100), text(body.get("contact"), "Contact", 3, 150),
        text(body.get("topic"), "Topic", 1, 100), text(body.get("message"), "Message", 10, 2000)))
    db().commit()
    return created({"ok": True})


# ---------- reports: public + resident ----------
@app.post("/api/reports")
@auth_required
def create_report():
    body = json_body()
    category = choice(body.get("category"), "Category", CATEGORIES)
    title = text(body.get("title"), "Title", 3, 90)
    description = text(body.get("description"), "Description", 10, 1200)
    location = text(body.get("location"), "Location", 1, 200, required=False)
    contact = text(body.get("contact"), "Contact", 1, 150, required=False)
    lat, lng = body.get("latitude"), body.get("longitude")
    if (lat is None) != (lng is None):
        fail(400, "Latitude and longitude must be provided together.")
    if lat is not None:
        lat, lng = number(lat, "Latitude", -90, 90), number(lng, "Longitude", -180, 180)

    for _ in range(20):
        reference = f"CT-{secrets.randbelow(900000) + 100000}"
        if report_by("reference", reference) is None:
            break
    else:
        fail(500, "Could not allocate a reference number. Please try again.")
    cur = db().execute("""INSERT INTO reports (reference, user_id, category, title, description,
                          location, latitude, longitude, contact) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (reference, g.user["id"], category, title, description, location, lat, lng, contact))
    add_note("report_updates", cur.lastrowid, "Report received. Our team will review it shortly.", "Submitted")
    db().commit()
    return created({"id": cur.lastrowid, "reference": reference, "status": "Submitted"})


@app.get("/api/reports")
def list_public():
    where, params = filters([("status", "Status", STATUSES, "r.status"),
                             ("category", "Category", CATEGORIES, "r.category")],
                            ["r.title", "r.description", "r.location", "r.reference"])
    where.append(f"r.published = 1 AND r.status NOT IN ({_sql(HIDDEN)})")
    return page(where, params, "r.updated_at DESC, r.id DESC", public_view)


@app.get("/api/reports/stats")
def stats():
    counts = dict(db().execute("SELECT status, COUNT(*) FROM reports GROUP BY status").fetchall())
    return jsonify({"inProgress": counts.get("In progress", 0), "resolved": counts.get("Resolved", 0),
                    "awaitingReview": counts.get("Submitted", 0) + counts.get("Under review", 0)})


@app.get("/api/reports/track/<reference>")
@rate_limit("track", 60, 900)
def track(reference):
    reference = reference.strip().upper()
    row = report_by("reference", reference) if REFERENCE_RE.match(reference) else None
    if row is None:
        fail(404, "We could not find a report with that reference number.")
    return jsonify(track_view(row))


@app.get("/api/reports/mine")
@auth_required
def mine():
    rows = db().execute("SELECT * FROM reports WHERE user_id = ? ORDER BY created_at DESC, id DESC",
                        (g.user["id"],)).fetchall()
    return jsonify({"total": len(rows), "items": [owner_view(r, updates=False) for r in rows]})


@app.get("/api/reports/<int:report_id>")
@auth_required
def get_report(report_id):
    row = own_or_staff(report_or_404(report_id))
    return jsonify(staff_view(row, detail=True) if g.user["role"] == "staff" else owner_view(row))


# ---------- photos (private; type verified from file contents) ----------
PHOTO_TYPES = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png"}
MIME = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def sniff(data):
    for magic, ext in PHOTO_TYPES.items():
        if data.startswith(magic):
            return ext
    return ".webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None


@app.post("/api/reports/<int:report_id>/photo")
@auth_required
def upload_photo(report_id):
    row = own_or_staff(report_or_404(report_id))
    upload = request.files.get("photo")
    if upload is None:
        fail(400, "Attach an image in the 'photo' field.")
    data = upload.read(MAX_PHOTO + 1)
    if len(data) > MAX_PHOTO:
        fail(413, "File too large (maximum 5 MB).")
    ext = sniff(data) or fail(400, "Photo must be a JPEG, PNG or WebP image.")
    filename = secrets.token_hex(16) + ext
    (UPLOAD_DIR / filename).write_bytes(data)
    db().execute(f"UPDATE reports SET photo_file = ?, updated_at = {NOW} WHERE id = ?", (filename, report_id))
    db().commit()
    if row["photo_file"]:
        (UPLOAD_DIR / os.path.basename(row["photo_file"])).unlink(missing_ok=True)
    return jsonify({"ok": True, "hasPhoto": True})


@app.get("/api/reports/<int:report_id>/photo")
@auth_required
def get_photo(report_id):
    row = own_or_staff(report_or_404(report_id))
    path = UPLOAD_DIR / os.path.basename(row["photo_file"] or "")
    if not row["photo_file"] or not path.is_file():
        fail(404, "This report has no photo.")
    resp = send_file(path, mimetype=MIME.get(path.suffix.lower(), "application/octet-stream"), max_age=0)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


# ---------- municipal staff ----------
@app.get("/api/staff/reports")
@staff_required
def queue():
    where, params = filters([("status", "Status", STATUSES, "r.status"),
                             ("category", "Category", CATEGORIES, "r.category"),
                             ("priority", "Priority", PRIORITIES, "r.priority"),
                             ("department", "Department", DEPARTMENTS, "r.department")],
                            ["r.title", "r.description", "r.location", "r.reference", "r.contact", "r.assignee"])
    order = "CASE r.priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, r.created_at DESC, r.id DESC"
    return page(where, params, order, staff_view)


@app.patch("/api/staff/reports/<int:report_id>")
@staff_required
def update_report(report_id):
    body = json_body()
    row = report_or_404(report_id)
    fields = {}  # column -> new value
    for key, label, allowed in (("status", "Status", STATUSES), ("priority", "Priority", PRIORITIES),
                                ("department", "Department", DEPARTMENTS)):
        if body.get(key) is not None:
            fields[key] = choice(body[key], label, allowed)
    if "assignee" in body:
        fields["assignee"] = text(body["assignee"], "Assignee", 1, 80, required=False)
    public_note = text(body.get("publicNote"), "Public note", 1, 1000, required=False)
    internal_note = text(body.get("internalNote"), "Internal note", 1, 1000, required=False)
    if not (fields or public_note or internal_note):
        fail(400, "Nothing to update.")

    status = fields.get("status")
    if status is not None and (status != row["status"] or status == "Resolved") and not public_note:
        fail(400, "A public note is required when changing the status or resolving a report.")
    if status is not None and status not in HIDDEN:
        fields["published"] = 1

    sets = "".join(f"{c} = ?, " for c in fields)
    db().execute(f"UPDATE reports SET {sets}updated_at = {NOW} WHERE id = ?", [*fields.values(), report_id])
    if public_note:
        add_note("report_updates", report_id, public_note, status or row["status"])
    if internal_note:
        add_note("internal_notes", report_id, internal_note)
    db().commit()
    return jsonify(staff_view(report_or_404(report_id), detail=True))


@app.post("/api/staff/reports/<int:report_id>/duplicate")
@staff_required
def mark_duplicate(report_id):
    row = report_or_404(report_id)
    reference = str(json_body().get("duplicateOf") or "").strip().upper()
    if not REFERENCE_RE.match(reference):
        fail(400, "duplicateOf must be a reference such as CT-123456.")
    if reference == row["reference"]:
        fail(400, "A report cannot be a duplicate of itself.")
    if report_by("reference", reference) is None:
        fail(404, "No report was found with that reference.")
    db().execute(f"UPDATE reports SET duplicate_of = ?, updated_at = {NOW} WHERE id = ?", (reference, report_id))
    add_note("internal_notes", report_id, f"Linked as a duplicate of {reference}.")
    db().commit()
    return jsonify(staff_view(report_or_404(report_id), detail=True))


@app.get("/api/staff/summary")
@staff_required
def summary():
    def counts(column, keys):
        found = dict(db().execute(f"SELECT {column}, COUNT(*) FROM reports GROUP BY {column}").fetchall())
        return {k: found.get(k, 0) for k in keys}
    by_status = counts("status", STATUSES)
    open_high = db().execute(f"SELECT COUNT(*) FROM reports WHERE priority = 'High' "
                             f"AND status NOT IN ({_sql(CLOSED)})").fetchone()[0]
    return jsonify({"total": sum(by_status.values()), "byStatus": by_status,
                    "byPriority": counts("priority", PRIORITIES), "openHighPriority": open_high})


@app.get("/api/staff/support")
@staff_required
def support_messages():
    rows = db().execute("SELECT id, name, contact, topic, message, created_at FROM support_messages "
                        "ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify({"total": len(rows),
                    "items": [pick(r, "id name contact topic message created_at") for r in rows]})


# ---------- command line ----------
def create_staff(name, email, password):
    email = email.strip().lower()
    if not name.strip() or not EMAIL_RE.match(email) or len(password) < 10:
        sys.exit("Usage: python civictrack.py create-staff NAME EMAIL PASSWORD  (password: 10+ characters)")
    with connect() as conn:
        found = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if found:
            conn.execute("UPDATE users SET name = ?, password_hash = ?, role = 'staff' WHERE id = ?",
                         (name.strip(), hash_password(password), found["id"]))
        else:
            conn.execute("INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, 'staff')",
                         (name.strip(), email, hash_password(password)))
    print(f"{'Updated' if found else 'Created'} staff account {email}.")


if __name__ == "__main__":
    if sys.argv[1:2] == ["create-staff"]:
        create_staff(*sys.argv[2:]) if len(sys.argv) == 5 else sys.exit(__doc__)
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        print(f"CivicTrack running at http://localhost:{PORT}")
        app.run(host=HOST, port=PORT, threaded=True, debug=False)
