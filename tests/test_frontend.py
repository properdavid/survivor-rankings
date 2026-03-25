"""Frontend smoke tests — SPA shell delivery, static files, unauthenticated API."""

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from app.auth import router as auth_router
from app.database import Base, get_db
from app.models import Season, Contestant, TribeConfig
from app.routes import router as api_router
from app.seed_data import SEASON_50_CONTESTANTS

TEST_SECRET = "test-frontend-secret"

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _override_get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


spa = FastAPI()
spa.add_middleware(SessionMiddleware, secret_key=TEST_SECRET)
spa.include_router(auth_router)
spa.include_router(api_router)
spa.mount("/static", StaticFiles(directory="static"), name="static")
spa.dependency_overrides[get_db] = _override_get_db


@spa.get("/")
def serve_index():
    return FileResponse("static/index.html")


client = TestClient(spa, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.create_all(bind=_engine)
    yield
    Base.metadata.drop_all(bind=_engine)


@pytest.fixture
def seeded_db():
    """DB with Season 50, its 24 contestants, and 3 tribe configs."""
    Base.metadata.create_all(bind=_engine)
    db = _Session()
    season = Season(number=50, name="Season 50", is_active=True)
    db.add(season)
    db.commit()
    db.refresh(season)
    for data in SEASON_50_CONTESTANTS:
        db.add(Contestant(season_id=season.id, **data))
    for tribe in [
        TribeConfig(season_id=season.id, name="Cila", color="#e67e22"),
        TribeConfig(season_id=season.id, name="Vatu", color="#2ecc71"),
        TribeConfig(season_id=season.id, name="Kalo", color="#9b59b6"),
    ]:
        db.add(tribe)
    db.commit()
    db.close()
    yield
    Base.metadata.drop_all(bind=_engine)


# ---------------------------------------------------------------------------
# SPA shell delivery
# ---------------------------------------------------------------------------

class TestSPAShell:
    def test_index_returns_200(self):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_index_content_type_is_html(self):
        resp = client.get("/")
        assert "text/html" in resp.headers.get("content-type", "")

    def test_index_contains_page_title(self):
        resp = client.get("/")
        assert "Survivor Rankings" in resp.text

    def test_index_links_app_js(self):
        resp = client.get("/")
        assert "app.js" in resp.text

    def test_index_links_style_css(self):
        resp = client.get("/")
        assert "style.css" in resp.text

    def test_index_has_season_select(self):
        resp = client.get("/")
        assert "season-select" in resp.text


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

class TestStaticFiles:
    def test_app_js_served(self):
        resp = client.get("/static/app.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers.get("content-type", "").lower()

    def test_style_css_served(self):
        resp = client.get("/static/style.css")
        assert resp.status_code == 200
        assert "css" in resp.headers.get("content-type", "").lower()

    def test_nonexistent_static_file_returns_404(self):
        resp = client.get("/static/does_not_exist.js")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Unauthenticated API behaviour
# ---------------------------------------------------------------------------

class TestUnauthenticated:
    def test_auth_me_returns_not_authenticated(self):
        resp = client.get("/auth/me")
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is False

    def test_leaderboard_public_with_season(self, seeded_db):
        seasons = client.get("/api/seasons").json()
        sid = seasons[0]["id"]
        resp = client.get(f"/api/leaderboard?season={sid}")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_contestants_requires_no_auth(self, seeded_db):
        seasons = client.get("/api/seasons").json()
        sid = seasons[0]["id"]
        resp = client.get(f"/api/contestants?season={sid}")
        assert resp.status_code == 200
        assert len(resp.json()) == 24

    def test_tribes_requires_no_auth(self, seeded_db):
        seasons = client.get("/api/seasons").json()
        sid = seasons[0]["id"]
        resp = client.get(f"/api/tribes?season={sid}")
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()}
        assert names == {"Cila", "Vatu", "Kalo"}

    def test_rankings_requires_auth(self, seeded_db):
        seasons = client.get("/api/seasons").json()
        sid = seasons[0]["id"]
        resp = client.get(f"/api/rankings?season={sid}")
        assert resp.status_code == 401

    def test_seasons_endpoint_public(self, seeded_db):
        resp = client.get("/api/seasons")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1
        assert resp.json()[0]["number"] == 50


# ---------------------------------------------------------------------------
# is_winner derived from elimination_order
# ---------------------------------------------------------------------------

class TestIsWinnerDerived:
    def test_no_winner_before_any_elimination(self, seeded_db):
        seasons = client.get("/api/seasons").json()
        sid = seasons[0]["id"]
        contestants = client.get(f"/api/contestants?season={sid}").json()
        assert all(not c["is_winner"] for c in contestants)

    def test_is_winner_true_for_highest_elimination_order(self, seeded_db):
        db = _Session()
        season = db.query(Season).first()
        winner = db.query(Contestant).filter(Contestant.season_id == season.id).first()
        winner.elimination_order = 24
        db.commit()
        db.close()

        seasons = client.get("/api/seasons").json()
        sid = seasons[0]["id"]
        contestants = client.get(f"/api/contestants?season={sid}").json()
        winner_entries = [c for c in contestants if c["is_winner"]]
        assert len(winner_entries) == 1
        assert winner_entries[0]["elimination_order"] == 24
