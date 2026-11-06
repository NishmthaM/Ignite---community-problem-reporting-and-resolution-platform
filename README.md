<<<<<<< HEAD
# CivicTrack (Python)

Community issue-reporting app: resident website, staff dashboard, REST API and SQLite, all in one Flask file (`civictrack.py`). Put the website (`index.html`, `css/`, `js/`) in `public/`.

## Quick start

```
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env     # set JWT_SECRET: python -c "import secrets; print(secrets.token_hex(48))"
python civictrack.py create-staff "Municipal Staff" staff@example.com "ChangeThisPassword123!"
python civictrack.py     # http://localhost:3000
```

The database and `uploads/` are created on first start (same schema as the Node version, so existing databases and bcrypt hashes keep working).

## Configuration (`.env`)

`PORT` (3000) · `HOST` (127.0.0.1) · `NODE_ENV` (`production` enables HSTS/HTTPS upgrade) · `JWT_SECRET` (32+ chars, required) · `JWT_EXPIRES_IN` (`8h`) · `DB_FILE` · `UPLOAD_DIR` · `FRONTEND_ORIGIN` (only if the site is on a different origin) · `TRUST_PROXY` (`1` behind a reverse proxy).

## Going live

Serve over HTTPS, set `NODE_ENV=production` and `TRUST_PROXY=1`, and run `gunicorn -w 1 --threads 8 civictrack:app` (or `waitress-serve civictrack:app`). Use one worker: rate-limit counters are in memory. Back up `data/` and `uploads/`.

## API (`/api`)

JSON in/out; auth is `Authorization: Bearer <token>`. Lists return `{total, items}`; errors return `{error}` (400, 401, 403, 404, 409, 413, 429).

| Endpoint | Notes |
|---|---|
| `GET /health`, `GET /meta` | status; categories, statuses, priorities, departments |
| `POST /auth/register` · `/auth/login` · `GET /auth/me` | `{name,email,password 8–128}` → `{token,user}`. Registration never grants staff. |
| `POST /support` | `{name,contact,topic,message}`; 5/hour per IP |
| `POST /reports` | resident. `{category,title 3–90,description 10–1200,location?,latitude?,longitude?,contact?}` → 201 `{id,reference,status}` |
| `GET /reports?status=&category=&q=&limit=&offset=` | public; only reviewed reports (not Submitted/Rejected); `status` may be comma-separated |
| `GET /reports/stats` · `/reports/track/CT-123456` | public counts; public status timeline |
| `GET /reports/mine` · `/reports/:id` | own reports; full report (owner or staff; staff also get notes and related) |
| `POST` / `GET /reports/:id/photo` | owner or staff; multipart `photo` (JPEG/PNG/WebP ≤ 5 MB, verified by content); private |
| `GET /staff/reports?status=&category=&priority=&department=&q=` | staff queue incl. contact data (limit ≤ 200) |
| `PATCH /staff/reports/:id` | `{status?,priority?,department?,assignee?,publicNote?,internalNote?}`; `publicNote` required on status change or Resolved |
| `POST /staff/reports/:id/duplicate` | `{duplicateOf:"CT-123456"}` |
| `GET /staff/summary` · `/staff/support` | totals by status/priority; latest support messages |

Statuses: Submitted, Under review, Accepted, Assigned, In progress, Resolved, Closed, Rejected.
=======
# Ignite---community-problem-reporting-and-resolution-platform
>>>>>>> a4c4eeb2e8130cfed3583f1dc5b3307f2227b354
