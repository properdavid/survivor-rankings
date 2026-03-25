"""Tests for the ranking audit log feature."""

import json
import base64
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from app.database import Base, get_db
from app.models import Season, Contestant, Ranking, TribeConfig, User, RankingAuditSubmission, RankingAuditEntry
from app.routes import router, get_client_ip

# ---------------------------------------------------------------------------
# Test app wired to an in-memory SQLite database
# ---------------------------------------------------------------------------

TEST_SECRET = "test-audit-secret"

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_session_cookie(data: dict) -> str:
    payload = base64.b64encode(json.dumps(data).encode())
    return TimestampSigner(TEST_SECRET).sign(payload).decode()


def client_for(user_id: int, is_admin: bool = False, email: str = "", name: str = "") -> TestClient:
    c = TestClient(test_app, raise_server_exceptions=True)
    session_data = {"user_id": user_id, "is_admin": is_admin}
    if email:
        session_data["user_email"] = email
    if name:
        session_data["user_name"] = name
    c.cookies.set("session", make_session_cookie(session_data))
    return c


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def mock_email(monkeypatch):
    monkeypatch.setattr("app.routes.send_rankings_email", lambda **kwargs: None)


@pytest.fixture
def db():
    session = TestingSession()
    yield session
    session.close()


def make_season(db, number=1, name="Test Season", active=True):
    s = Season(number=number, name=name, is_active=active)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def make_contestants(db, count=6, season=None):
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


def make_user(db, admin=False, email=None, name=None):
    if email is None:
        email = f"{'admin' if admin else 'user'}@test.com"
    if name is None:
        name = email.split("@")[0]
    u = User(email=email, name=name, is_admin=admin)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def sp(season):
    return f"?season={season.id}"


def submit_rankings(user, season, contestants, c=None):
    """Helper: submit rankings for a user and return the response."""
    if c is None:
        c = client_for(user.id, user.is_admin, email=user.email, name=user.name)
    payload = {"rankings": [{"contestant_id": ct.id, "rank": i + 1} for i, ct in enumerate(contestants)]}
    return c.post(f"/api/rankings{sp(season)}", json=payload)


# ---------------------------------------------------------------------------
# get_client_ip tests
# ---------------------------------------------------------------------------

class TestGetClientIp:
    def test_cf_connecting_ip_preferred(self, db):
        """CF-Connecting-IP header should be used when present."""
        season = make_season(db)
        contestants = make_contestants(db, season=season)
        user = make_user(db)
        c = client_for(user.id, email=user.email, name=user.name)
        payload = {"rankings": [{"contestant_id": ct.id, "rank": i + 1} for i, ct in enumerate(contestants)]}
        c.post(f"/api/rankings{sp(season)}", json=payload, headers={"CF-Connecting-IP": "203.0.113.42"})

        sess = TestingSession()
        sub = sess.query(RankingAuditSubmission).first()
        assert sub.client_ip == "203.0.113.42"
        sess.close()

    def test_x_forwarded_for_fallback(self, db):
        """X-Forwarded-For first IP should be used when no CF header."""
        season = make_season(db)
        contestants = make_contestants(db, season=season)
        user = make_user(db)
        c = client_for(user.id, email=user.email, name=user.name)
        payload = {"rankings": [{"contestant_id": ct.id, "rank": i + 1} for i, ct in enumerate(contestants)]}
        c.post(f"/api/rankings{sp(season)}", json=payload, headers={"X-Forwarded-For": "10.0.0.1, 10.0.0.2"})

        sess = TestingSession()
        sub = sess.query(RankingAuditSubmission).first()
        assert sub.client_ip == "10.0.0.1"
        sess.close()

    def test_cf_takes_priority_over_xff(self, db):
        """CF-Connecting-IP should win over X-Forwarded-For."""
        season = make_season(db)
        contestants = make_contestants(db, season=season)
        user = make_user(db)
        c = client_for(user.id, email=user.email, name=user.name)
        payload = {"rankings": [{"contestant_id": ct.id, "rank": i + 1} for i, ct in enumerate(contestants)]}
        c.post(f"/api/rankings{sp(season)}", json=payload, headers={
            "CF-Connecting-IP": "203.0.113.42",
            "X-Forwarded-For": "10.0.0.1",
        })

        sess = TestingSession()
        sub = sess.query(RankingAuditSubmission).first()
        assert sub.client_ip == "203.0.113.42"
        sess.close()

    def test_fallback_to_client_host(self, db):
        """Falls back to request.client.host when no proxy headers."""
        season = make_season(db)
        contestants = make_contestants(db, season=season)
        user = make_user(db)
        c = client_for(user.id, email=user.email, name=user.name)
        payload = {"rankings": [{"contestant_id": ct.id, "rank": i + 1} for i, ct in enumerate(contestants)]}
        c.post(f"/api/rankings{sp(season)}", json=payload)

        sess = TestingSession()
        sub = sess.query(RankingAuditSubmission).first()
        assert sub.client_ip is not None
        assert sub.client_ip != "unknown"
        sess.close()


# ---------------------------------------------------------------------------
# Audit log creation tests
# ---------------------------------------------------------------------------

class TestAuditCreation:
    def test_submit_creates_audit_submission(self, db):
        season = make_season(db)
        contestants = make_contestants(db, season=season)
        user = make_user(db)
        res = submit_rankings(user, season, contestants)
        assert res.status_code == 200

        sess = TestingSession()
        subs = sess.query(RankingAuditSubmission).all()
        assert len(subs) == 1
        assert subs[0].user_id == user.id
        assert subs[0].season_id == season.id
        assert subs[0].contestant_count == len(contestants)
        sess.close()

    def test_audit_entries_match_rankings(self, db):
        season = make_season(db)
        contestants = make_contestants(db, season=season)
        user = make_user(db)
        submit_rankings(user, season, contestants)

        sess = TestingSession()
        entries = sess.query(RankingAuditEntry).order_by(RankingAuditEntry.rank).all()
        assert len(entries) == len(contestants)
        for i, entry in enumerate(entries):
            assert entry.rank == i + 1
            assert entry.contestant_name == contestants[i].name
        sess.close()

    def test_audit_captures_session_metadata(self, db):
        season = make_season(db)
        contestants = make_contestants(db, season=season)
        user = make_user(db, email="monica@test.com", name="Monica")
        c = client_for(user.id, email="monica@test.com", name="Monica")
        payload = {"rankings": [{"contestant_id": ct.id, "rank": i + 1} for i, ct in enumerate(contestants)]}
        c.post(f"/api/rankings{sp(season)}", json=payload)

        sess = TestingSession()
        sub = sess.query(RankingAuditSubmission).first()
        assert sub.session_user_email == "monica@test.com"
        assert sub.session_user_name == "Monica"
        sess.close()

    def test_multiple_saves_create_separate_submissions(self, db):
        season = make_season(db)
        contestants = make_contestants(db, season=season)
        user = make_user(db)
        # First save
        submit_rankings(user, season, contestants)
        # Second save (reverse order)
        submit_rankings(user, season, list(reversed(contestants)))

        sess = TestingSession()
        subs = sess.query(RankingAuditSubmission).all()
        assert len(subs) == 2
        sess.close()


# ---------------------------------------------------------------------------
# Admin endpoint tests
# ---------------------------------------------------------------------------

class TestAuditAdminEndpoints:
    def test_audit_list_requires_admin(self, db):
        season = make_season(db)
        user = make_user(db)
        anon = TestClient(test_app, raise_server_exceptions=True)
        res = anon.get(f"/api/admin/audit/rankings?user_id={user.id}{sp(season).replace('?', '&')}")
        assert res.status_code == 401

    def test_audit_list_returns_submissions(self, db):
        season = make_season(db)
        contestants = make_contestants(db, season=season)
        user = make_user(db, email="player@test.com", name="Player")
        admin = make_user(db, admin=True, email="admin@test.com")
        submit_rankings(user, season, contestants)

        admin_client = client_for(admin.id, is_admin=True)
        res = admin_client.get(f"/api/admin/audit/rankings?user_id={user.id}{sp(season).replace('?', '&')}")
        assert res.status_code == 200
        data = res.json()
        assert len(data) == 1
        assert data[0]["user_name"] == "Player"
        assert data[0]["contestant_count"] == len(contestants)

    def test_audit_list_scoped_to_season(self, db):
        s1 = make_season(db, number=1, name="S1", active=True)
        c1 = make_contestants(db, count=3, season=s1)
        user = make_user(db)
        admin = make_user(db, admin=True, email="admin@test.com")
        submit_rankings(user, s1, c1)

        # Now create s2, deactivate s1
        s2 = make_season(db, number=2, name="S2", active=True)
        s1.is_active = False
        db.commit()
        c2 = make_contestants(db, count=3, season=s2)
        submit_rankings(user, s2, c2)

        admin_client = client_for(admin.id, is_admin=True)
        res = admin_client.get(f"/api/admin/audit/rankings?user_id={user.id}&season={s1.id}")
        assert len(res.json()) == 1
        res2 = admin_client.get(f"/api/admin/audit/rankings?user_id={user.id}&season={s2.id}")
        assert len(res2.json()) == 1

    def test_audit_detail_returns_snapshot(self, db):
        season = make_season(db)
        contestants = make_contestants(db, season=season)
        user = make_user(db)
        admin = make_user(db, admin=True, email="admin@test.com")
        submit_rankings(user, season, contestants)

        sess = TestingSession()
        sub = sess.query(RankingAuditSubmission).first()
        sub_id = sub.id
        sess.close()

        admin_client = client_for(admin.id, is_admin=True)
        res = admin_client.get(f"/api/admin/audit/rankings/{sub_id}")
        assert res.status_code == 200
        data = res.json()
        assert len(data["entries"]) == len(contestants)
        assert data["entries"][0]["rank"] == 1

    def test_audit_detail_not_found_returns_404(self, db):
        make_season(db)
        admin = make_user(db, admin=True)
        admin_client = client_for(admin.id, is_admin=True)
        res = admin_client.get("/api/admin/audit/rankings/999")
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestAuditEdgeCases:
    def test_no_audit_for_failed_submission(self, db):
        season = make_season(db)
        make_contestants(db, count=6, season=season)
        user = make_user(db)
        c = client_for(user.id, email=user.email, name=user.name)
        # Submit wrong number of rankings
        res = c.post(f"/api/rankings{sp(season)}", json={"rankings": [{"contestant_id": 1, "rank": 1}]})
        assert res.status_code == 400

        sess = TestingSession()
        assert sess.query(RankingAuditSubmission).count() == 0
        sess.close()

    def test_audit_ordered_newest_first(self, db):
        season = make_season(db)
        contestants = make_contestants(db, season=season)
        user = make_user(db)
        admin = make_user(db, admin=True, email="admin@test.com")
        # Two saves
        submit_rankings(user, season, contestants)
        submit_rankings(user, season, list(reversed(contestants)))

        admin_client = client_for(admin.id, is_admin=True)
        res = admin_client.get(f"/api/admin/audit/rankings?user_id={user.id}{sp(season).replace('?', '&')}")
        data = res.json()
        assert len(data) == 2
        # Newest first
        assert data[0]["id"] > data[1]["id"]
