# 📚 EduVault V1 — School Digital Library System

A web-based digital library system built for schools, allowing faculty to upload study materials and students to browse and view them — all inside the browser, no downloads needed.

---

## Why V1 Exists

Built as a fully working prototype to test the core concept before building a production system. Deployed and tested at a real school. Limitations discovered during real use directly shaped the design of V2.

## What I Learned From V1

- Predefined classes and subjects don't work across different schools
- No admin panel means no way to manage users or library structure
- These findings led directly to V2 — with a full admin panel and dynamic class/subject configuration

---

## Features

- 🎒 **Student access** — zero login, just pick class and subject
- 🧑‍🏫 **Faculty registration and login** — secure session-based auth
- 📤 **File upload** — stored in Gmail as email attachments (free, 15 GB)
- 👁 **In-browser viewer** — PDF, PPT, DOC viewed inside the site, never leaves
- 🔍 **Search** — filter files by name within a subject
- 📋 **Faculty dashboard** — upload, browse, delete own files with stats
- 🔄 **Rotating logs** — 5 MB × 5 backups via Python logging

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.10+ · Flask |
| Database | SQLite (metadata only) |
| File Storage | Gmail via SMTP + IMAP (free) |
| Frontend | HTML · CSS · Vanilla JavaScript |
| Auth | Flask sessions · SHA-256 password hashing |
| Logging | Python RotatingFileHandler |

---

## Project Structure

```
eduvault-v1/
├── app.py                  ← Flask backend (all routes + Gmail logic)
├── .env                    ← Gmail credentials (never commit — see .env.example)
├── .env.example            ← Template for .env
├── .gitignore
├── README.md
├── eduvault.db             ← Auto-created on first run (metadata only)
├── logs/
│   └── eduvault.log        ← Auto-created rotating log
└── static/
    ├── index.html           ← Entry point — Student or Faculty
    ├── student.html         ← Class → Subject → File list
    ├── faculty-login.html   ← Faculty sign in
    ├── faculty-register.html← Faculty 2-step registration
    ├── faculty-profile.html ← Dashboard — upload + manage files
    └── pdf-viewer.html      ← In-browser file viewer
```

---

## Setup & Run

### 1. Install dependencies
```bash
pip install flask werkzeug python-dotenv
```

### 2. Set up Gmail App Password
```
1. Go to myaccount.google.com → Security
2. Turn ON 2-Step Verification
3. Search "App Passwords"
4. App: Mail | Device: Other → type "EduVault"
5. Generate → copy the 16-character password
```

### 3. Configure .env
```bash
cp .env.example .env
# Open .env and fill in your Gmail app password
```

### 4. Run
```bash
python app.py
# Open http://localhost:5000
```

---

## API Routes

| Method | Route | Auth | Purpose |
|---|---|---|---|
| POST | `/api/faculty/register` | ✗ | Create faculty account |
| POST | `/api/faculty/login` | ✗ | Start session |
| POST | `/api/faculty/logout` | ✓ | End session |
| GET | `/api/faculty/me` | ✓ | Current user profile |
| POST | `/api/files/upload` | ✓ | Upload file → Gmail |
| GET | `/api/files` | ✗ | List files (filterable) |
| GET | `/api/files/my` | ✓ | Faculty's own uploads |
| GET | `/api/files/<id>/download` | ✗ | Stream file from Gmail |
| DELETE | `/api/files/<id>` | ✓ | Delete own file |
| GET | `/api/stats` | ✗ | Library statistics |

---

## How Gmail Storage Works

```
Faculty uploads file
    → app.py reads file into memory
    → Sends it as email attachment to your Gmail
    → Gmail Message-ID saved in SQLite
    → No file stored on server disk

Student views file
    → app.py fetches email from Gmail via IMAP
    → Streams file bytes directly to browser
    → Student sees file inline, never downloads
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `GMAIL_ADDRESS` | Gmail account used for storage |
| `GMAIL_APP_PASS` | 16-char Gmail App Password |
| `EDUVAULT_SECRET` | Flask session secret key |
| `PORT` | Server port (default 5000) |
| `FLASK_DEBUG` | `true` only in development |

---

## Limitations (addressed in V2)

- Classes and subjects are predefined — not configurable per school
- No admin panel — no way to manage faculty accounts
- No cross-school support — built for a single school structure

## See Also

→ **EduVault V2** *(coming soon)* — production version with admin panel, dynamic classes/subjects, multi-school support
