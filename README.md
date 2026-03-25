# 🔥 Survivor Rankings

A web application where users predict the elimination order of Survivor contestants and earn points based on how well their predictions match reality. Supports multiple seasons, live scoring, and a competitive leaderboard.

## Features

- **Multi-Season** - Create and manage multiple seasons; switch between them from the footer
- **Google OAuth** - Sign in with your Google account
- **Drag & Drop Rankings** - Rank all contestants (1 = predicted winner, N = first out)
- **Live Scoring** - Points awarded as contestants are eliminated
- **Leaderboard** - Compete with friends and family
- **Episode Discussions** - Per-episode discussion threads with posts and reactions
- **Email Rankings** - Rankings are emailed to you automatically on save, or on demand via "Email to Me"
- **Admin Panel** - Record eliminations, manage tribes/seasons/users, database backup/restore, audit log
- **Mobile Friendly** - Responsive design with collapsible header on mobile
- **PWA** - Install on your phone's home screen for a native app feel
- **Docker Ready** - Deploy easily on a home server

## Scoring

Scoring works differently for the **Final 3** versus all other castaways.

### Final 3 — Special Scoring

The last three castaways are scored on whether you identified them as top contenders:

| Contestant | Your Predicted Rank | Points |
|------------|---------------------|--------|
| Winner | #1 | 20 pts |
| Winner | #2 or #3 | 10 pts |
| Winner | #4 or lower | Sliding scale (0–10 pts) |
| Runner-up or 3rd place | #1, #2, or #3 | 10 pts |
| Runner-up or 3rd place | #4 or lower | Sliding scale (0–10 pts) |

### Standard Scoring — Everyone Else

The sliding scale applies to all castaways eliminated before the Final 3, and to any Final 3 castaway you ranked outside your top 3. Points are based on how close your predicted rank is to their actual finish position.

| Positions Off | Points |
|---------------|--------|
| Exact match | 10 pts |
| Off by 1 | 9 pts |
| Off by 2 | 8 pts |
| ... | ... |
| Off by 9 | 1 pt |
| Off by 10+ | 0 pts |

**Maximum possible score: 250 points** (20 for the winner + 10 each for 2nd/3rd place + 10 each for the remaining 21 castaways, assuming 24 contestants)

### Removed Contestants

If a castaway is pulled from the game for medical or other reasons (rather than being voted out), they are marked as **Removed**. No points are awarded for removed contestants regardless of how you ranked them.

For contestants who leave *after* a removal, scoring uses a **window** rather than a single target position. Any rank that correctly reflects either the departure slot (counting the removal) or the voted-out position (ignoring it) scores a perfect 10 pts. Ranks outside the window use the normal sliding scale.

### Late Submissions

If a user submits rankings after the season has already started (i.e., one or more contestants have been eliminated or removed), their submission is accepted but any contestants who have already departed are marked as **ineligible for scoring**. Those picks appear as "Late pick" in the scores breakdown and earn **0 points**. All remaining contestants are scored normally.

## Setup

### 1. Google OAuth Credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Create a new project (or select existing)
3. Go to **APIs & Services > Credentials**
4. Click **Create Credentials > OAuth client ID**
5. Choose **Web application**
6. Add authorized redirect URIs:
   - For local dev: `http://localhost:8000/auth/callback`
   - For your server: `http://YOUR_SERVER_IP:8000/auth/callback`
7. Copy the **Client ID** and **Client Secret**

### 2. Configure Environment

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
# Edit .env with your Google OAuth credentials, secret key, and admin email
```

> **Tip:** Generate a secret key with: `python -c "import secrets; print(secrets.token_hex(32))"`

**Email (optional):** To enable ranking email notifications, add Gmail SMTP credentials to `.env`:
```
SMTP_EMAIL=your-gmail@gmail.com
SMTP_PASSWORD=your-app-password
```
Get a Gmail App Password at https://myaccount.google.com/apppasswords. If not configured, the email feature is silently skipped.

### 3. Deploy with Docker

```bash
# Build and start
docker compose up -d --build

# View logs
docker compose logs -f

# Stop (data is safe in ./data/)
docker compose down
```

The app will be available at `http://localhost:8000` (or your server's IP on port 8000).

The `./data` folder is created automatically and holds the SQLite database. It persists across container restarts — pulling a new image or running `docker compose down` will never erase user data.

### 4. Run Locally (Development)

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run
uvicorn app.main:app --reload --port 8000

# Run tests
python -m pytest tests/ -v
```

## Usage

### For Players
1. Sign in with Google
2. Drag and drop contestants to set your rankings (1 = predicted winner)
3. Click **Save Rankings**
4. Rankings lock once the admin records the first elimination
5. Check the **Scores** tab to see your points as the season progresses
6. Check the **Leaderboard** to see how you compare
7. Use the season selector in the footer to view past seasons (read-only)

> **Late submissions:** If you submit rankings after eliminations have already started, your picks are accepted but you won't earn points for contestants who have already been eliminated or removed.

### For the Admin
1. Sign in with the admin email set in `ADMIN_EMAIL`
2. Go to the **Admin** tab
3. **Contestant Management** — As each contestant leaves the game, enter their departure number and click **Eliminate** (voted out) or **Remove** (medical/other). Use **Winner** to mark the season winner. All departures share a single numbered sequence.
4. **User Management** — Promote/demote admins, view or clear user rankings, jump to audit log
5. **Tribe Management** — Create, recolor, or delete tribes
6. **Season Management** — Create new seasons and set the active season
7. **Database** — Download a full database backup or restore from a previous backup
8. **Audit Log** — View the full history of every ranking submission per user (timestamps, IP addresses, session identity, ranking snapshots). Useful for investigating disputes or session issues.

## Architecture

```
├── app/
│   ├── main.py          # FastAPI app, startup migrations, data seeding, cache busting
│   ├── config.py         # Environment config (OAuth, SMTP, database)
│   ├── database.py       # SQLite + SQLAlchemy setup
│   ├── models.py         # User, Season, Contestant, Ranking, TribeConfig,
│   │                     # EpisodeThread, DiscussionPost, PostReaction,
│   │                     # RankingAuditSubmission, RankingAuditEntry
│   ├── auth.py           # Google OAuth routes
│   ├── routes.py         # API endpoints (all season-scoped via ?season=<id>)
│   ├── scoring.py        # Scoring logic with typed interfaces
│   ├── email.py          # Rankings email (Gmail SMTP)
│   └── seed_data.py      # Season 50 contestant data
├── static/
│   ├── index.html        # Single page app shell
│   ├── app.js            # Frontend logic (season switching, rankings, admin)
│   ├── style.css         # Styling (tribe colors injected dynamically)
│   ├── manifest.json     # PWA manifest
│   ├── sw.js             # Service worker (PWA install support)
│   └── icon-*.png        # Home screen icons
├── tests/
│   ├── test_scoring.py   # Scoring engine unit tests
│   ├── test_routes.py    # API integration tests
│   ├── test_frontend.py  # SPA smoke tests
│   ├── test_email.py     # Email utility tests
│   ├── test_discussion.py # Discussion thread/post/reaction tests
│   └── test_audit.py     # Ranking audit log tests
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## Reverse Proxy (Optional)

If you want to serve behind nginx with HTTPS:

```nginx
server {
    listen 443 ssl;
    server_name survivor.yourdomain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Update `BASE_URL` in your `.env`:
```
BASE_URL=https://survivor.yourdomain.com
```

And add `https://survivor.yourdomain.com/auth/callback` to your Google OAuth redirect URIs.
