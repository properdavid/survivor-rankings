"""Route tests covering the API endpoints in app/routes.py.

All endpoints are season-scoped. Test fixtures create a Season and pass
?season=<id> to every API call. Tests are grouped by feature area.
"""

import json
import base64
import os
import sqlite3
import tempfile
from typing import Optional
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from app.database import Base, get_db
from app.models import Season, Contestant, Ranking, TribeConfig, User
from app.routes import router

# ---------------------------------------------------------------------------
# Test app wired to an in-memory SQLite database
# ---------------------------------------------------------------------------

TEST_SECRET = "test-secret"

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


test_app = FastAPI()
test_app.add_middleware(SessionMiddleware, secret_key=TEST_SECRET)
test_app.include_router(router)
test_app.dependency_overrides[get_db] = override_get_db

client = TestClient(test_app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_session_cookie(data: dict) -> str:
    payload = base64.b64encode(json.dumps(data).encode())
    return TimestampSigner(TEST_SECRET).sign(payload).decode()


def cookies(user_id: int, is_admin: bool = False) -> dict:
    return {"session": make_session_cookie({"user_id": user_id, "is_admin": is_admin})}


def client_for(user_id: int, is_admin: bool = False) -> TestClient:
    c = TestClient(test_app, raise_server_exceptions=True)
    c.cookies.set("session", make_session_cookie({"user_id": user_id, "is_admin": is_admin}))
    return c


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def mock_email(monkeypatch):
    """Prevent tests from sending real emails."""
    monkeypatch.setattr("app.routes.send_rankings_email", lambda **kwargs: None)


@pytest.fixture
def db():
    session = TestingSession()
    yield session
    session.close()


def make_season(db, number: int = 1, name: str = "Test Season", active: bool = True) -> Season:
    s = Season(number=number, name=name, is_active=active)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def make_contestants(db, count: int = 6, season: Optional[Season] = None) -> list[Contestant]:
    if season is None:
        season = make_season(db)
    db.add(TribeConfig(season_id=season.id, name="Alpha", color="#ff0000"))
    cs = [Contestant(season_id=season.id, name=f"C{i}", tribe="Alpha") for i in range(1, count + 1)]
    for c in cs:
        db.add(c)
    db.commit()
    for c in cs:
        db.refresh(c)
    return cs


def make_user(db, admin: bool = False, email: Optional[str] = None) -> User:
    if email is None:
        email = f"{'admin' if admin else 'user'}@test.com"
    u = User(email=email, name=email.split("@")[0], is_admin=admin)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def sp(season: Season) -> str:
    """Return the ?season=<id> query string for a season."""
    return f"?season={season.id}"


# ---------------------------------------------------------------------------
# is_winner derived from elimination_order
# ---------------------------------------------------------------------------

def _get_contestant_from_api(contestant_id: int, season: Season) -> dict:
    data = client.get(f"/api/contestants{sp(season)}").json()
    return next(c for c in data if c["id"] == contestant_id)


class TestIsWinnerDerivation:
    def test_last_departure_is_winner_in_api_response(self, db):
        season = make_season(db)
        cs = make_contestants(db, count=6, season=season)
        admin = make_user(db, admin=True)

        resp = client.post(
            f"/api/admin/eliminate{sp(season)}",
            json={"contestant_id": cs[-1].id, "elimination_order": 6},
            cookies=cookies(admin.id, is_admin=True),
        )
        assert resp.status_code == 200
        assert _get_contestant_from_api(cs[-1].id, season)["is_winner"] is True

    def test_non_last_departure_not_winner(self, db):
        season = make_season(db)
        cs = make_contestants(db, count=6, season=season)
        admin = make_user(db, admin=True)

        client.post(
            f"/api/admin/eliminate{sp(season)}",
            json={"contestant_id": cs[0].id, "elimination_order": 1},
            cookies=cookies(admin.id, is_admin=True),
        )
        assert _get_contestant_from_api(cs[0].id, season)["is_winner"] is False

    def test_frontend_is_winner_field_is_ignored(self, db):
        season = make_season(db)
        cs = make_contestants(db, count=6, season=season)
        admin = make_user(db, admin=True)

        client.post(
            f"/api/admin/eliminate{sp(season)}",
            json={"contestant_id": cs[0].id, "elimination_order": 1, "is_winner": True},
            cookies=cookies(admin.id, is_admin=True),
        )
        assert _get_contestant_from_api(cs[0].id, season)["is_winner"] is False


# ---------------------------------------------------------------------------
# remove_contestant
# ---------------------------------------------------------------------------

class TestRemoveContestant:
    def test_response_message_contains_departure_number(self, db):
        season = make_season(db)
        cs = make_contestants(db, count=6, season=season)
        admin = make_user(db, admin=True)

        resp = client.post(
            f"/api/admin/remove-contestant{sp(season)}",
            json={"contestant_id": cs[0].id, "elimination_order": 3},
            cookies=cookies(admin.id, is_admin=True),
        )
        assert resp.status_code == 200
        assert "3" in resp.json()["message"]

    def test_zero_elimination_order_rejected(self, db):
        season = make_season(db)
        cs = make_contestants(db, count=6, season=season)
        admin = make_user(db, admin=True)

        resp = client.post(
            f"/api/admin/remove-contestant{sp(season)}",
            json={"contestant_id": cs[0].id, "elimination_order": 0},
            cookies=cookies(admin.id, is_admin=True),
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# TribeColorUpdate
# ---------------------------------------------------------------------------

class TestUpdateTribeColor:
    def test_valid_color_updates_successfully(self, db):
        season = make_season(db)
        make_contestants(db, season=season)
        admin = make_user(db, admin=True)
        tribe = db.query(TribeConfig).filter(TribeConfig.season_id == season.id).first()

        resp = client.patch(
            f"/api/admin/tribes/{tribe.id}",
            json={"color": "#1a2b3c"},
            cookies=cookies(admin.id, is_admin=True),
        )
        assert resp.status_code == 200
        db.refresh(tribe)
        assert tribe.color == "#1a2b3c"

    def test_missing_color_returns_422(self, db):
        season = make_season(db)
        make_contestants(db, season=season)
        admin = make_user(db, admin=True)
        tribe = db.query(TribeConfig).filter(TribeConfig.season_id == season.id).first()

        resp = client.patch(
            f"/api/admin/tribes/{tribe.id}",
            json={},
            cookies=cookies(admin.id, is_admin=True),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# RankingItem model — submit_rankings
# ---------------------------------------------------------------------------

class TestSubmitRankings:
    def test_valid_submission_saves_all_rankings(self, db):
        season = make_season(db)
        cs = make_contestants(db, count=6, season=season)
        user = make_user(db)
        payload = [{"contestant_id": c.id, "rank": i + 1} for i, c in enumerate(cs)]

        resp = client.post(
            f"/api/rankings{sp(season)}",
            json={"rankings": payload},
            cookies=cookies(user.id),
        )
        assert resp.status_code == 200
        saved = db.query(Ranking).filter(Ranking.user_id == user.id, Ranking.season_id == season.id).all()
        assert len(saved) == 6

    def test_wrong_contestant_count_rejected(self, db):
        season = make_season(db)
        cs = make_contestants(db, count=6, season=season)
        user = make_user(db)
        payload = [{"contestant_id": cs[0].id, "rank": 1}]

        resp = client.post(
            f"/api/rankings{sp(season)}",
            json={"rankings": payload},
            cookies=cookies(user.id),
        )
        assert resp.status_code == 400

    def test_late_submission_marks_departed_ineligible(self, db):
        season = make_season(db)
        cs = make_contestants(db, count=6, season=season)
        admin = make_user(db, admin=True)
        user = make_user(db, email="player@test.com")

        admin_c = client_for(admin.id, is_admin=True)
        user_c = client_for(user.id)

        admin_c.post(
            f"/api/admin/eliminate{sp(season)}",
            json={"contestant_id": cs[0].id, "elimination_order": 1},
        )

        payload = [{"contestant_id": c.id, "rank": i + 1} for i, c in enumerate(cs)]
        resp = user_c.post(f"/api/rankings{sp(season)}", json={"rankings": payload})

        assert resp.status_code == 200
        assert resp.json().get("late_submission") is True
        db.expire_all()
        rankings = db.query(Ranking).filter(Ranking.user_id == user.id, Ranking.season_id == season.id).all()
        ineligible = [r for r in rankings if not r.scoring_eligible]
        assert len(ineligible) == 1
        assert ineligible[0].contestant_id == cs[0].id

    def test_locked_rankings_cannot_be_resubmitted(self, db):
        season = make_season(db)
        cs = make_contestants(db, count=6, season=season)
        admin = make_user(db, admin=True)
        payload = [{"contestant_id": c.id, "rank": i + 1} for i, c in enumerate(cs)]

        client.post(f"/api/rankings{sp(season)}", json={"rankings": payload}, cookies=cookies(admin.id))

        client.post(
            f"/api/admin/eliminate{sp(season)}",
            json={"contestant_id": cs[0].id, "elimination_order": 1},
            cookies=cookies(admin.id, is_admin=True),
        )

        resp = client.post(f"/api/rankings{sp(season)}", json={"rankings": payload}, cookies=cookies(admin.id))
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------

class TestLeaderboard:
    def test_empty_when_no_rankings(self, db):
        season = make_season(db)
        resp = client.get(f"/api/leaderboard{sp(season)}")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_sorted_by_total_score_descending(self, db):
        season = make_season(db)
        cs = make_contestants(db, count=6, season=season)
        admin = make_user(db, admin=True)
        user_a = make_user(db, email="a@test.com")
        user_b = make_user(db, email="b@test.com")

        admin_c = client_for(admin.id, is_admin=True)
        client_a = client_for(user_a.id)
        client_b = client_for(user_b.id)

        payload_a = [{"contestant_id": cs[i].id, "rank": 6 - i} for i in range(6)]
        assert client_a.post(f"/api/rankings{sp(season)}", json={"rankings": payload_a}).status_code == 200

        payload_b = [{"contestant_id": cs[i].id, "rank": i + 1} for i in range(6)]
        assert client_b.post(f"/api/rankings{sp(season)}", json={"rankings": payload_b}).status_code == 200

        admin_c.post(
            f"/api/admin/eliminate{sp(season)}",
            json={"contestant_id": cs[0].id, "elimination_order": 1},
        )

        board = client.get(f"/api/leaderboard{sp(season)}").json()
        assert len(board) == 2
        assert board[0]["user_name"] == "a"
        assert board[0]["total_score"] > board[1]["total_score"]


# ---------------------------------------------------------------------------
# Season CRUD
# ---------------------------------------------------------------------------

class TestSeasonCRUD:
    def test_list_seasons(self, db):
        make_season(db, number=50, name="Season 50")
        resp = client.get("/api/seasons")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["number"] == 50

    def test_create_season(self, db):
        admin = make_user(db, admin=True)
        resp = client.post(
            "/api/admin/seasons",
            json={"number": 51, "name": "Season 51"},
            cookies=cookies(admin.id, is_admin=True),
        )
        assert resp.status_code == 200
        assert resp.json()["number"] == 51

    def test_activate_season(self, db):
        s1 = make_season(db, number=50, name="Season 50", active=True)
        s2 = make_season(db, number=51, name="Season 51", active=False)
        admin = make_user(db, admin=True)

        resp = client.post(
            f"/api/admin/seasons/{s2.id}/activate",
            cookies=cookies(admin.id, is_admin=True),
        )
        assert resp.status_code == 200
        db.expire_all()
        assert s1.is_active is False
        assert s2.is_active is True

    def test_past_season_rejects_ranking_submission(self, db):
        season = make_season(db, active=False)
        cs = make_contestants(db, count=6, season=season)
        user = make_user(db)
        payload = [{"contestant_id": c.id, "rank": i + 1} for i, c in enumerate(cs)]

        resp = client.post(
            f"/api/rankings{sp(season)}",
            json={"rankings": payload},
            cookies=cookies(user.id),
        )
        assert resp.status_code == 400
        assert "not active" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Cross-season isolation
# ---------------------------------------------------------------------------

class TestCrossSeasonIsolation:
    def test_contestants_scoped_to_season(self, db):
        s1 = make_season(db, number=50, name="Season 50")
        s2 = make_season(db, number=51, name="Season 51", active=False)
        make_contestants(db, count=6, season=s1)
        make_contestants(db, count=3, season=s2)

        resp1 = client.get(f"/api/contestants{sp(s1)}")
        resp2 = client.get(f"/api/contestants{sp(s2)}")
        assert len(resp1.json()) == 6
        assert len(resp2.json()) == 3

    def test_elimination_locks_only_own_season(self, db):
        s1 = make_season(db, number=50, name="Season 50")
        s2 = make_season(db, number=51, name="Season 51", active=True)
        cs1 = make_contestants(db, count=6, season=s1)
        cs2 = make_contestants(db, count=6, season=s2)
        admin = make_user(db, admin=True)
        user = make_user(db, email="player@test.com")

        # User submits rankings in both seasons
        payload1 = [{"contestant_id": c.id, "rank": i + 1} for i, c in enumerate(cs1)]
        payload2 = [{"contestant_id": c.id, "rank": i + 1} for i, c in enumerate(cs2)]
        client.post(f"/api/rankings{sp(s1)}", json={"rankings": payload1}, cookies=cookies(user.id))
        client.post(f"/api/rankings{sp(s2)}", json={"rankings": payload2}, cookies=cookies(user.id))

        # Eliminate in s1
        client.post(
            f"/api/admin/eliminate{sp(s1)}",
            json={"contestant_id": cs1[0].id, "elimination_order": 1},
            cookies=cookies(admin.id, is_admin=True),
        )

        # s1 rankings should be locked
        db.expire_all()
        s1_rankings = db.query(Ranking).filter(Ranking.season_id == s1.id, Ranking.user_id == user.id).all()
        assert all(r.locked for r in s1_rankings)

        # s2 rankings should NOT be locked
        s2_rankings = db.query(Ranking).filter(Ranking.season_id == s2.id, Ranking.user_id == user.id).all()
        assert all(not r.locked for r in s2_rankings)


# ---------------------------------------------------------------------------
# Email Rankings
# ---------------------------------------------------------------------------

class TestEmailRankings:
    def test_email_rankings_requires_auth(self, db):
        season = make_season(db)
        anon = TestClient(test_app, raise_server_exceptions=True)
        resp = anon.post(f"/api/rankings/email{sp(season)}")
        assert resp.status_code == 401

    def test_email_rankings_no_rankings_returns_400(self, db):
        season = make_season(db)
        make_contestants(db, count=6, season=season)
        user = make_user(db)
        resp = client.post(f"/api/rankings/email{sp(season)}", cookies=cookies(user.id))
        assert resp.status_code == 400
        assert "No rankings saved" in resp.json()["detail"]

    @patch("app.routes.send_rankings_email")
    def test_email_rankings_success(self, mock_send, db):
        season = make_season(db)
        cs = make_contestants(db, count=6, season=season)
        user = make_user(db)

        payload = [{"contestant_id": c.id, "rank": i + 1} for i, c in enumerate(cs)]
        client.post(f"/api/rankings{sp(season)}", json={"rankings": payload}, cookies=cookies(user.id))
        mock_send.reset_mock()

        resp = client.post(f"/api/rankings/email{sp(season)}", cookies=cookies(user.id))
        assert resp.status_code == 200
        assert "email" in resp.json()["message"].lower()
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        assert call_kwargs[1]["to_email"] == user.email
        assert call_kwargs[1]["season_name"] == season.name
        assert len(call_kwargs[1]["rankings"]) == 6

    @patch("app.routes.send_rankings_email")
    def test_save_rankings_triggers_email(self, mock_send, db):
        season = make_season(db)
        cs = make_contestants(db, count=6, season=season)
        user = make_user(db)

        payload = [{"contestant_id": c.id, "rank": i + 1} for i, c in enumerate(cs)]
        resp = client.post(
            f"/api/rankings{sp(season)}",
            json={"rankings": payload},
            cookies=cookies(user.id),
        )
        assert resp.status_code == 200
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        assert call_kwargs[1]["to_email"] == user.email


# ---------------------------------------------------------------------------
# Database Export/Import
# ---------------------------------------------------------------------------

def _make_temp_sqlite():
    """Create a temporary valid SQLite database file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    return path


class TestDatabaseExportImport:
    def test_export_requires_admin(self, db):
        make_season(db)
        anon = TestClient(test_app, raise_server_exceptions=True)
        resp = anon.get("/api/admin/database/export")
        assert resp.status_code == 401

    @patch("app.routes.get_db_path")
    def test_export_returns_sqlite_file(self, mock_path, db):
        make_season(db)
        admin = make_user(db, admin=True)
        tmp = _make_temp_sqlite()
        try:
            mock_path.return_value = tmp
            c = client_for(admin.id, is_admin=True)
            resp = c.get("/api/admin/database/export")
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "application/octet-stream"
            assert "survivor-backup-" in resp.headers.get("content-disposition", "")
            assert resp.content[:16] == b"SQLite format 3\x00"
        finally:
            os.unlink(tmp)

    def test_import_requires_admin(self, db):
        make_season(db)
        anon = TestClient(test_app, raise_server_exceptions=True)
        resp = anon.post("/api/admin/database/import", files={"file": ("test.db", b"fake", "application/octet-stream")})
        assert resp.status_code == 401

    def test_import_rejects_invalid_file(self, db):
        make_season(db)
        admin = make_user(db, admin=True)
        c = client_for(admin.id, is_admin=True)
        resp = c.post("/api/admin/database/import", files={"file": ("test.db", b"not a sqlite file", "application/octet-stream")})
        assert resp.status_code == 400
        assert "not a SQLite" in resp.json()["detail"]

    @patch("app.routes.engine")
    @patch("app.routes.get_db_path")
    def test_import_replaces_database(self, mock_path, mock_engine, db):
        make_season(db)
        admin = make_user(db, admin=True)
        fd, target = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        mock_path.return_value = target

        tmp = _make_temp_sqlite()
        try:
            with open(tmp, "rb") as f:
                upload_data = f.read()
            c = client_for(admin.id, is_admin=True)
            resp = c.post("/api/admin/database/import", files={"file": ("backup.db", upload_data, "application/octet-stream")})
            assert resp.status_code == 200
            assert "restored" in resp.json()["message"].lower()
            mock_engine.dispose.assert_called_once()
            with open(target, "rb") as f:
                assert f.read()[:16] == b"SQLite format 3\x00"
        finally:
            os.unlink(tmp)
            if os.path.exists(target):
                os.unlink(target)
