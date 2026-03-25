# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

```bash
# Run locally (development with auto-reload)
uvicorn app.main:app --reload --port 8000

# Run with Docker
docker compose up -d --build
docker compose logs -f
docker compose down   # data persists in ./data/

# Install dependencies
pip install -r requirements.txt

# Run tests
python3 -m pytest tests/ -v
```

## Architecture

FastAPI backend + vanilla JavaScript SPA + SQLite database. No build step — static files are served directly. Installable as a PWA on mobile.

**Backend** (`app/`):
- `main.py` — App entry point, startup migrations (ALTER TABLE for new/dropped columns, season backfill), and data seeding
- `config.py` — Environment variables (Google OAuth creds, SECRET_KEY, ADMIN_EMAIL, DATABASE_URL)
- `auth.py` — Google OAuth 2.0 flow (Authlib), session-based auth with 30-day cookies
- `routes.py` — All API endpoints under `/api/*` and admin endpoints under `/api/admin/*`. Every data endpoint accepts `?season=<id>` (defaults to the active season)
- `scoring.py` — Scoring engine with Final 3 special scoring, late submission handling, and typed interfaces (`RankingInput`, `BreakdownEntry`, `ScoreResult`)
- `models.py` — SQLAlchemy models: User, Season, Contestant, Ranking, TribeConfig
- `seed_data.py` — 24 Season 50 contestants across 3 tribes (Cila, Vatu, Kalo)

**Frontend** (`static/`):
- `index.html` — SPA shell with tab sections (Rankings, Scores, Leaderboard, How to Play, Admin)
- `app.js` — All client logic: auth, season switching, drag-and-drop ranking, scoring display, leaderboard, admin panel (contestants, users, tribes, seasons)
- `style.css` — Styling with dynamic tribe color injection via JS (no hardcoded tribe CSS)
- `manifest.json` — PWA manifest for "Add to Home Screen" support
- `sw.js` — Minimal service worker (enables PWA install prompt, no offline caching)
- `icon-192.png`, `icon-512.png` — PWA home screen icons

**Tests** (`tests/`):
- `test_scoring.py` — Unit tests for the scoring engine (sliding scale, Final 3, removals, late submissions)
- `test_routes.py` — 26 integration tests for API routes (season CRUD, cross-season isolation, rankings, leaderboard, admin endpoints)
- `test_frontend.py` — 17 smoke tests for SPA delivery, static files, unauthenticated API behaviour, and is_winner derivation
- `test_discussion.py` — 31 tests for episode discussions (threads, posts, reactions, display names, episode count)

## Key Patterns

**Multi-season architecture**: All data is scoped by season. The `Season` model has `id`, `number`, `name`, `is_active`. Contestant, TribeConfig, and Ranking all have a `season_id` FK. The `get_season()` dependency in `routes.py` resolves the season from the `?season=` query param, defaulting to `is_active=True`. Admin write endpoints reject non-active seasons (past seasons are read-only).

**Database migrations**: No Alembic. Schema changes are applied via `ALTER TABLE` in `main.py`'s `startup()` function — check column/table existence with `inspect(engine)`, then execute raw SQL if missing. The startup also handles the Season 50 backfill: creates the season record, then `UPDATE ... SET season_id = :sid WHERE season_id IS NULL` on all tables.

**Ranking lifecycle**: Users submit rankings (1=predicted winner, N=predicted first out) for the active season. Rankings lock per-season when the admin records the first elimination/removal in that season. Eliminating in Season 50 does NOT lock Season 51 rankings. Late first-time submissions are allowed but contestants already departed get `scoring_eligible=False`.

**Scoring system**: Two tiers — Final 3 special scoring (20pts winner pick, 10pts finalist pick) and standard sliding scale (10pts exact to 0pts at 10+ off). Points are awarded for correctly predicting the order in which contestants are eliminated by vote. Removed contestants (non-vote departures) score 0 points and do not affect other contestants' scoring. Late submissions also score 0. The scoring engine (`scoring.py`) is season-agnostic — callers pass `total_contestants` which is derived per-season.

**`is_winner` is computed, not stored**: The `is_winner` field in API responses is derived from `elimination_order == total_contestants` at the API layer. There is no `is_winner` column in the database. This eliminates the possibility of the two fields drifting out of sync.

**Unified departure sequence**: All departures (eliminations + removals) share a single 1–N numbered sequence per season. `elimination_order` tracks position in this sequence for both eliminated and removed contestants. Finish position = total_contestants + 1 − elimination_order.

**Frontend state**: Module-level variables (`currentUser`, `currentSeason`, `seasons`, `contestants`, `tribes`, `myRankings`). DOM is re-rendered on state changes. No framework — uses `innerHTML` templates with `escapeHtml()` for XSS prevention. All API fetch calls include `seasonParam()` which appends `?season=<id>`.

**Season switching**: A `<select>` dropdown in the footer lets users switch between seasons. `switchSeason()` reloads contestants, tribes, rankings, and refreshes the active tab. Past seasons show a read-only banner and hide save/submit buttons.

**Compact header (mobile)**: On viewports ≤768px, the header compresses when scrolling down. Uses a headroom check to prevent oscillation on borderline-length pages — the header only enters compact mode if the document is tall enough to remain scrolled after the header shrinks.

**Asset versioning**: CSS and JS references in `index.html` use `?v={{CACHE_VERSION}}` placeholders. At startup, `main.py` replaces these with a timestamp (`CACHE_VERSION = str(int(time.time()))`), so every deploy automatically busts the Cloudflare cache — **no manual `?v=` bumping needed**. The footer shows a human-readable version (e.g. `v30`) that is decoupled from cache busting. Two versioning tiers for the footer/changelog:
- **Feature releases** (integer bump, e.g. `v29` → `v30`): For new features or significant changes. Update the footer version. Add a `CHANGELOG.md` entry describing the feature and a user-friendly entry to the `WHATS_NEW` array in `app.js`. WHATS_NEW should only describe things regular users will notice (new UI features, ranking email, etc.). Backend-only, admin-only, or infrastructure changes should be summarized generically (e.g. "Bug fixes & improvements") rather than described in detail.
- **Bug fix releases** (0.1 increment, e.g. `v29` → `v29.1`): For CSS fixes, typo corrections, or minor patches. Update the footer version. Add a short `CHANGELOG.md` entry (e.g. "Minor bug fixes"). No `WHATS_NEW` entry needed.

**Episode discussions**: Per-episode threads created by admin (`EpisodeThread` model with `season_id` FK and UniqueConstraint on `(season_id, episode_number)`). Flat chronological posts (`DiscussionPost`) with 500-char limit. Reactions (`PostReaction`) use a one-per-user-per-post UniqueConstraint — toggle on/off/switch via a single endpoint. Posts show `display_name` (first name + last initial) computed server-side by `format_display_name()`. Past-season discussions are read-only. Paginated at 25 posts per page. `Season.episode_count` (nullable int) optionally caps thread creation.

**Image proxy**: `/api/image-proxy` fetches external contestant photos, crops to face region with Pillow, and caches in memory (LRU, max 500 entries).

**PWA**: Manifest + minimal service worker enable "Add to Home Screen" on iOS and Android. No offline caching — the app requires the server for all data. Icons are generated programmatically via Pillow.
