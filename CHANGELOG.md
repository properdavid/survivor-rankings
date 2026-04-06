# Changelog

All notable changes to this project are documented here.

## v31

**Bonus Questions**
- New "Bonus Questions" tab lets players answer optional questions for extra points throughout the season
- Two scoring types: Standard (fixed points for correct/partial answers) and Wager (risk points for bigger gains or losses)
- Configurable per-question deadlines in Pacific time — answers lock automatically after the deadline
- Players can update their answer any time before the deadline
- All answers are hidden until the deadline passes (including from admins)
- Admin panel section to create questions, set scoring parameters, and grade submitted answers
- Bonus points are included in each player's score and leaderboard total

## v30

**Automatic Cache Busting**
- Static asset URLs now use an auto-generated timestamp instead of a manual version number
- Every deploy automatically busts the Cloudflare cache — no more stale CSS/JS after deploys

## v29.1

Minor bug fixes — admin sub-tabs now scroll horizontally on mobile instead of overflowing the page.

## v29

**Pacific Time Timestamps**
- All timestamps displayed on the site (audit log, discussion posts) and in ranking emails now show in Pacific time (PDT/PST) instead of UTC

## v28

**Ranking Audit Log**
- Every ranking submission is now logged with full metadata: timestamp, client IP (Cloudflare-aware), user-agent, session identity, and the complete ranking snapshot
- New "Audit Log" tab in the Admin Panel to browse submission history per user
- Session mismatch detection: submissions where the session identity doesn't match the user are flagged with a red warning badge
- "Audit" button on each user row in User Management for quick access to their submission history

## v27

**Session Security Fix**
- Added `Cache-Control: no-store, private` to all API, auth, and index responses to prevent proxies (Cloudflare, Caddy) from caching authenticated responses
- Session cookies now use `Secure` flag (`https_only=True`) for proper HTTPS-only transmission
- Fixes an issue where a caching proxy could serve one user's session cookie to another user

## v26

**Database Backup & Restore**
- New "Database" tab in the Admin Panel for backup and restore
- Download a full database backup file with one click
- Restore from a backup by uploading a previously exported file
- SQLite file validation prevents importing invalid files

## v25

**Email Rankings**
- Rankings are automatically emailed to you when you save
- New "Email to Me" button in the rankings save area sends your current rankings on demand
- HTML-formatted email with tribe colors, numbered ranking list, and season branding
- Email sending runs in the background — saving is never slowed down
- Gracefully skipped when SMTP is not configured (development/local environments)

## v24

**What's New modal**
- Clicking the version number in the footer now opens a "What's New" modal with user-friendly release notes
- Version link styled subtly in the footer (text-muted, underline on hover)

## v23

**Multiple reactions per post**
- Users can now select any combination of reactions (like, heart, sad) on a single post instead of being limited to one
- Each reaction type toggles independently — clicking a reaction adds it, clicking again removes it

## v22

**Episode Discussion**
- Added Discussion tab with per-episode threads for talking about each episode
- Admin creates episode threads as episodes air (episode number + title)
- Flat chronological posts with 500-character limit
- Reactions on posts: like, heart, and sad face (one per user per post, toggle on/off/switch)
- Users can edit their own posts (marked as edited); only admins can delete posts
- Posts labeled with user's first name + last initial and Google profile picture
- Pagination: 25 posts per page with Previous/Next controls
- Episode list shows post counts per thread
- Static spoiler warning at the top of the Discussion tab
- Past-season discussions are read-only (compose, edit, and react UI hidden)
- New `episode_count` field on seasons (configurable in Season Management admin sub-tab)
- New database models: `EpisodeThread`, `DiscussionPost`, `PostReaction`
- 31 new tests covering threads, posts, reactions, display names, and episode count

## v21

**Pull-to-refresh for PWA**
- Added touch-based pull-to-refresh gesture for standalone PWA mode
- Spinner slides out from under the header when pulling down from the top of the page
- Refreshes contestants, tribes, and the active tab's data without a full page reload
- Skips activation during drag-and-drop ranking reorders to prevent conflicts

## v20

**Scroll oscillation fix and PWA support**
- Fixed mobile header oscillation bug on iPhone — the compact header would stutter rapidly on pages with borderline content height (e.g., leaderboard with exactly 4 entries). Added a headroom check that prevents compact mode when the document isn't tall enough to stay scrolled after the header shrinks
- Added Progressive Web App support: `manifest.json`, service worker (`sw.js`), and home screen icons (192px and 512px)
- Added `<meta name="theme-color">` and `<link rel="apple-touch-icon">` for native app feel on iOS and Android

## v19

**Season selector moved to footer**
- Moved the season `<select>` dropdown from the navigation bar to the footer
- Added `.footer-content` flexbox layout to position the selector alongside the version text

## v18

**Multi-season support**
- Added `Season` model with `id`, `number`, `name`, `is_active` fields
- Added `season_id` foreign key to `Contestant`, `TribeConfig`, and `Ranking` models
- Startup migration backfills existing data into Season 50 automatically
- All API endpoints now accept `?season=<id>` query parameter (defaults to the active season)
- Added `get_season()` dependency and `require_active_season()` guard — past seasons are read-only
- New endpoints: `GET /api/seasons`, `POST /api/admin/seasons`, `POST /api/admin/seasons/{id}/activate`
- Frontend: `currentSeason` state, `seasonParam()` helper appended to all fetch calls
- Season switcher dropdown, dynamic header text (`SURVIVOR <number>`), dynamic page title
- Past seasons show a read-only banner and hide save/submit buttons
- Admin panel gains "Season Management" sub-tab for creating and activating seasons
- Removed hardcoded "Season 50" and "24" references — all dynamic from season and contestant data
- Removed static `.tribe-cila/vatu/kalo` CSS rules (already superseded by `injectTribeStyles()`)
- Rankings lock/unlock is now scoped per-season — eliminating in one season doesn't lock another

## v17

**Starting state — single-season app**

This was the baseline version before the refactoring session. Changes made at v17 (before the version was bumped):
- Removed dead `reset_elimination` endpoint
- Fixed N+1 query in leaderboard — replaced per-user query loop with a single three-way JOIN
- Added Pydantic models for all admin endpoints that previously used raw `dict` input (`ContestantTribeUpdate`, `TribeCreate`, `TribeColorUpdate`, `RemoveContestantRequest`, `ResetContestantRequest`)
- Added `RankingItem` model to replace `list[dict]` in `RankingSubmission`
- Added bounded LRU eviction to the in-memory image cache (max 500 entries)
- Derived `is_winner` from `elimination_order` in API responses instead of reading from DB column
- Dropped the `is_winner` column from the database schema with a startup migration
- Removed redundant `eliminated_count` re-query in `update_elimination`
- Added `TypedDict` interfaces to `scoring.py`: `RankingInput`, `BreakdownEntry`, `ScoreResult`
- Eliminated the separate `total_contestants` count query in the leaderboard — derived from ranking data
- Fixed a `NameError` bug in `remove_contestant` response message (`elimination_order` → `data.elimination_order`)
- Added `pytest` to dependencies and created three test files:
  - `test_scoring.py` — 21 unit tests for the scoring engine
  - `test_routes.py` — integration tests for all API routes
  - `test_frontend.py` — SPA smoke tests for static files, auth, and is_winner derivation
