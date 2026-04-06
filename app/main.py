"""Main FastAPI application."""

import time

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import inspect, text

from app.config import SECRET_KEY
from app.database import engine, SessionLocal
from app.models import Base, User, Season, Contestant, Ranking, TribeConfig, EpisodeThread, DiscussionPost, PostReaction, RankingAuditSubmission, RankingAuditEntry
from app.auth import router as auth_router
from app.routes import router as api_router
from app.seed_data import SEASON_50_CONTESTANTS

app = FastAPI(title="Survivor Rankings", version="2.0.0")

# Session middleware for OAuth — https_only ensures cookies are only sent over HTTPS
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400 * 30, https_only=True)


@app.middleware("http")
async def add_cache_control(request: Request, call_next):
    """Prevent proxies (Cloudflare, Caddy) from caching authenticated responses."""
    response: Response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/") or path.startswith("/auth/") or path == "/":
        response.headers["Cache-Control"] = "no-store, private"
    return response


# Include routers
app.include_router(auth_router)
app.include_router(api_router)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
def startup():
    """Create tables, run migrations, and seed data on startup."""
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    table_names = inspector.get_table_names()

    with engine.connect() as conn:
        # --- Migrate existing single-season databases to multi-season ---

        # Legacy column cleanup
        if "contestants" in table_names:
            contestant_cols = {col["name"] for col in inspector.get_columns("contestants")}
            if "is_removed" not in contestant_cols:
                conn.execute(text("ALTER TABLE contestants ADD COLUMN is_removed BOOLEAN NOT NULL DEFAULT 0"))
                conn.commit()
            if "is_winner" in contestant_cols:
                conn.execute(text("ALTER TABLE contestants DROP COLUMN is_winner"))
                conn.commit()

            # Add season_id FK to contestants if missing
            if "season_id" not in contestant_cols:
                conn.execute(text("ALTER TABLE contestants ADD COLUMN season_id INTEGER REFERENCES seasons(id)"))
                conn.commit()

        if "rankings" in table_names:
            ranking_cols = {col["name"] for col in inspector.get_columns("rankings")}
            if "scoring_eligible" not in ranking_cols:
                conn.execute(text("ALTER TABLE rankings ADD COLUMN scoring_eligible BOOLEAN NOT NULL DEFAULT 1"))
                conn.commit()
            if "season_id" not in ranking_cols:
                conn.execute(text("ALTER TABLE rankings ADD COLUMN season_id INTEGER REFERENCES seasons(id)"))
                conn.commit()

        if "tribe_configs" in table_names:
            tribe_cols = {col["name"] for col in inspector.get_columns("tribe_configs")}
            if "season_id" not in tribe_cols:
                conn.execute(text("ALTER TABLE tribe_configs ADD COLUMN season_id INTEGER REFERENCES seasons(id)"))
                conn.commit()

        if "seasons" in table_names:
            season_cols = {col["name"] for col in inspector.get_columns("seasons")}
            if "episode_count" not in season_cols:
                conn.execute(text("ALTER TABLE seasons ADD COLUMN episode_count INTEGER"))
                conn.commit()

        if "bonus_questions" in table_names:
            bq_cols = {col["name"] for col in inspector.get_columns("bonus_questions")}
            if "answer_type" not in bq_cols:
                conn.execute(text("ALTER TABLE bonus_questions ADD COLUMN answer_type VARCHAR"))
                conn.commit()

        # Migrate post_reactions: old constraint was (post_id, user_id), new is (post_id, user_id, reaction_type)
        if "post_reactions" in table_names:
            # Check if the old unique constraint exists by inspecting unique constraints
            uniques = inspector.get_unique_constraints("post_reactions")
            old_constraint = any(
                set(u["column_names"]) == {"post_id", "user_id"} for u in uniques
            )
            if old_constraint:
                conn.execute(text("""
                    CREATE TABLE post_reactions_new (
                        id INTEGER PRIMARY KEY,
                        post_id INTEGER NOT NULL REFERENCES discussion_posts(id),
                        user_id INTEGER NOT NULL REFERENCES users(id),
                        reaction_type VARCHAR NOT NULL,
                        UNIQUE (post_id, user_id, reaction_type)
                    )
                """))
                conn.execute(text("""
                    INSERT INTO post_reactions_new (id, post_id, user_id, reaction_type)
                    SELECT id, post_id, user_id, reaction_type FROM post_reactions
                """))
                conn.execute(text("DROP TABLE post_reactions"))
                conn.execute(text("ALTER TABLE post_reactions_new RENAME TO post_reactions"))
                conn.execute(text("CREATE INDEX ix_post_reactions_id ON post_reactions (id)"))
                conn.commit()

    # --- Seed Season 50 and backfill existing data ---
    db = SessionLocal()
    try:
        # Create Season 50 if it doesn't exist
        season50 = db.query(Season).filter(Season.number == 50).first()
        if not season50:
            season50 = Season(number=50, name="Season 50", is_active=True)
            db.add(season50)
            db.commit()
            db.refresh(season50)

        # Backfill season_id on any existing rows that are missing it
        with engine.connect() as conn:
            conn.execute(text(
                "UPDATE contestants SET season_id = :sid WHERE season_id IS NULL"
            ), {"sid": season50.id})
            conn.execute(text(
                "UPDATE rankings SET season_id = :sid WHERE season_id IS NULL"
            ), {"sid": season50.id})
            conn.execute(text(
                "UPDATE tribe_configs SET season_id = :sid WHERE season_id IS NULL"
            ), {"sid": season50.id})
            conn.commit()

        # Seed contestants if none exist for Season 50
        s50_contestants = db.query(Contestant).filter(Contestant.season_id == season50.id).count()
        if s50_contestants == 0:
            for contestant_data in SEASON_50_CONTESTANTS:
                contestant = Contestant(season_id=season50.id, **contestant_data)
                db.add(contestant)
            db.commit()
        else:
            # Patch image_url for any contestant that is missing it
            seed_by_name = {c["name"]: c for c in SEASON_50_CONTESTANTS}
            updated = False
            for contestant in db.query(Contestant).filter(Contestant.season_id == season50.id).all():
                if not contestant.image_url and contestant.name in seed_by_name:
                    contestant.image_url = seed_by_name[contestant.name].get("image_url")
                    updated = True
            if updated:
                db.commit()

        # Seed tribe configs if none exist for Season 50
        s50_tribes = db.query(TribeConfig).filter(TribeConfig.season_id == season50.id).count()
        if s50_tribes == 0:
            for tribe in [
                TribeConfig(season_id=season50.id, name="Cila", color="#e67e22"),
                TribeConfig(season_id=season50.id, name="Vatu", color="#2ecc71"),
                TribeConfig(season_id=season50.id, name="Kalo", color="#9b59b6"),
            ]:
                db.add(tribe)
            db.commit()


        # Backfill audit log for users who have rankings but no audit entries
        users_with_rankings = (
            db.query(Ranking.user_id, Ranking.season_id)
            .group_by(Ranking.user_id, Ranking.season_id)
            .all()
        )
        for uid, sid in users_with_rankings:
            existing_audit = db.query(RankingAuditSubmission).filter(
                RankingAuditSubmission.user_id == uid,
                RankingAuditSubmission.season_id == sid,
            ).first()
            if existing_audit:
                continue
            user = db.query(User).filter(User.id == uid).first()
            rankings = (
                db.query(Ranking, Contestant)
                .join(Contestant, Ranking.contestant_id == Contestant.id)
                .filter(Ranking.user_id == uid, Ranking.season_id == sid)
                .order_by(Ranking.rank)
                .all()
            )
            if not rankings:
                continue
            audit_sub = RankingAuditSubmission(
                user_id=uid,
                season_id=sid,
                session_user_email=user.email if user else None,
                session_user_name=user.name if user else None,
                client_ip="backfill",
                user_agent="startup backfill",
                contestant_count=len(rankings),
                created_at=rankings[0][0].created_at,
            )
            db.add(audit_sub)
            db.flush()
            for r, c in rankings:
                db.add(RankingAuditEntry(
                    submission_id=audit_sub.id,
                    contestant_id=c.id,
                    contestant_name=c.name,
                    rank=r.rank,
                ))
            db.commit()
    finally:
        db.close()


CACHE_VERSION = str(int(time.time()))

with open("static/index.html") as _f:
    _index_html = _f.read().replace("{{CACHE_VERSION}}", CACHE_VERSION)


@app.get("/")
async def serve_index():
    return HTMLResponse(_index_html)
