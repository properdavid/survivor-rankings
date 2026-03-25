"""Tests for the episode discussion feature.

Covers thread CRUD, posts (create, edit, delete, pagination),
reactions (toggle on/off/switch), display name formatting,
and episode count management.
"""

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
from app.models import Season, User, EpisodeThread, DiscussionPost, PostReaction
from app.routes import router, format_display_name

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
    """Return a fresh TestClient with the given user's session cookie pre-set."""
    c = TestClient(test_app, raise_server_exceptions=True)
    c.cookies.set("session", make_session_cookie({"user_id": user_id, "is_admin": is_admin}))
    return c


def anon_client() -> TestClient:
    """Return a fresh TestClient with no cookies."""
    return TestClient(test_app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = TestingSession()
    yield session
    session.close()


def make_season(db, number: int = 1, name: str = "Test Season", active: bool = True,
                episode_count: Optional[int] = None) -> Season:
    s = Season(number=number, name=name, is_active=active, episode_count=episode_count)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def make_user(db, admin: bool = False, email: Optional[str] = None,
              name: Optional[str] = None) -> User:
    if email is None:
        email = f"{'admin' if admin else 'user'}@test.com"
    if name is None:
        name = email.split("@")[0]
    u = User(email=email, name=name, is_admin=admin, picture="https://example.com/pic.jpg")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def make_thread(db, season: Season, episode_number: int = 1, title: str = "Episode 1") -> EpisodeThread:
    t = EpisodeThread(season_id=season.id, episode_number=episode_number, title=title)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def sp(season: Season) -> str:
    return f"?season={season.id}"


# ---------------------------------------------------------------------------
# Thread tests
# ---------------------------------------------------------------------------

class TestEpisodeThreads:
    def test_create_thread_as_admin(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        c = client_for(admin.id, is_admin=True)
        resp = c.post(
            f"/api/admin/discussions{sp(season)}",
            json={"episode_number": 1, "title": "Premiere"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["episode_number"] == 1
        assert data["title"] == "Premiere"
        assert data["post_count"] == 0

    def test_create_thread_requires_admin(self, db):
        season = make_season(db)
        user = make_user(db)
        c = client_for(user.id)
        resp = c.post(
            f"/api/admin/discussions{sp(season)}",
            json={"episode_number": 1, "title": "Premiere"},
        )
        assert resp.status_code == 403

    def test_create_thread_requires_active_season(self, db):
        season = make_season(db, active=False)
        admin = make_user(db, admin=True)
        c = client_for(admin.id, is_admin=True)
        resp = c.post(
            f"/api/admin/discussions{sp(season)}",
            json={"episode_number": 1, "title": "Premiere"},
        )
        assert resp.status_code == 400

    def test_duplicate_episode_number_rejected(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        make_thread(db, season, episode_number=1)
        c = client_for(admin.id, is_admin=True)
        resp = c.post(
            f"/api/admin/discussions{sp(season)}",
            json={"episode_number": 1, "title": "Duplicate"},
        )
        assert resp.status_code == 400

    def test_episode_number_exceeds_season_limit(self, db):
        season = make_season(db, episode_count=13)
        admin = make_user(db, admin=True)
        c = client_for(admin.id, is_admin=True)
        resp = c.post(
            f"/api/admin/discussions{sp(season)}",
            json={"episode_number": 14, "title": "Too many"},
        )
        assert resp.status_code == 400

    def test_list_threads_with_post_counts(self, db):
        season = make_season(db)
        user = make_user(db)
        t1 = make_thread(db, season, episode_number=1, title="Ep 1")
        t2 = make_thread(db, season, episode_number=2, title="Ep 2")
        for i in range(3):
            db.add(DiscussionPost(thread_id=t1.id, user_id=user.id, content=f"Post {i}"))
        db.commit()

        c = anon_client()
        resp = c.get(f"/api/discussions{sp(season)}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["episode_number"] == 1
        assert data[0]["post_count"] == 3
        assert data[1]["episode_number"] == 2
        assert data[1]["post_count"] == 0

    def test_rename_thread(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        thread = make_thread(db, season, title="Old Title")
        c = client_for(admin.id, is_admin=True)
        resp = c.patch(
            f"/api/admin/discussions/{thread.id}",
            json={"title": "New Title"},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "New Title"

    def test_rename_requires_admin(self, db):
        season = make_season(db)
        user = make_user(db)
        thread = make_thread(db, season)
        c = client_for(user.id)
        resp = c.patch(
            f"/api/admin/discussions/{thread.id}",
            json={"title": "Hacked"},
        )
        assert resp.status_code == 403

    def test_delete_thread(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        user = make_user(db, email="user@test.com")
        thread = make_thread(db, season)
        db.add(DiscussionPost(thread_id=thread.id, user_id=user.id, content="A post"))
        db.commit()

        c = client_for(admin.id, is_admin=True)
        resp = c.delete(f"/api/admin/discussions/{thread.id}")
        assert resp.status_code == 200
        assert db.query(EpisodeThread).filter(EpisodeThread.id == thread.id).first() is None
        assert db.query(DiscussionPost).filter(DiscussionPost.thread_id == thread.id).count() == 0

    def test_delete_requires_admin(self, db):
        season = make_season(db)
        user = make_user(db)
        thread = make_thread(db, season)
        c = client_for(user.id)
        resp = c.delete(f"/api/admin/discussions/{thread.id}")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Post tests
# ---------------------------------------------------------------------------

class TestDiscussionPosts:
    def test_create_post(self, db):
        season = make_season(db)
        user = make_user(db, name="John Doe")
        thread = make_thread(db, season)
        c = client_for(user.id)
        resp = c.post(
            f"/api/discussions/{thread.id}/posts{sp(season)}",
            json={"content": "Great episode!"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == "Great episode!"
        assert data["display_name"] == "John D."
        assert data["is_edited"] is False

    def test_create_post_requires_auth(self, db):
        season = make_season(db)
        thread = make_thread(db, season)
        c = anon_client()
        resp = c.post(
            f"/api/discussions/{thread.id}/posts{sp(season)}",
            json={"content": "Hello"},
        )
        assert resp.status_code == 401

    def test_create_post_500_char_limit(self, db):
        season = make_season(db)
        user = make_user(db)
        thread = make_thread(db, season)
        c = client_for(user.id)
        resp = c.post(
            f"/api/discussions/{thread.id}/posts{sp(season)}",
            json={"content": "x" * 501},
        )
        assert resp.status_code == 400
        assert "500" in resp.json()["detail"]

    def test_create_post_exactly_500_chars(self, db):
        season = make_season(db)
        user = make_user(db)
        thread = make_thread(db, season)
        c = client_for(user.id)
        resp = c.post(
            f"/api/discussions/{thread.id}/posts{sp(season)}",
            json={"content": "x" * 500},
        )
        assert resp.status_code == 200

    def test_create_post_readonly_season(self, db):
        season = make_season(db, active=False)
        user = make_user(db)
        thread = make_thread(db, season)
        c = client_for(user.id)
        resp = c.post(
            f"/api/discussions/{thread.id}/posts{sp(season)}",
            json={"content": "Hello"},
        )
        assert resp.status_code == 400

    def test_create_post_empty_content_rejected(self, db):
        season = make_season(db)
        user = make_user(db)
        thread = make_thread(db, season)
        c = client_for(user.id)
        resp = c.post(
            f"/api/discussions/{thread.id}/posts{sp(season)}",
            json={"content": "   "},
        )
        assert resp.status_code == 400

    def test_get_posts_paginated(self, db):
        season = make_season(db)
        user = make_user(db)
        thread = make_thread(db, season)
        for i in range(30):
            db.add(DiscussionPost(thread_id=thread.id, user_id=user.id, content=f"Post {i}"))
        db.commit()

        c = anon_client()
        resp = c.get(f"/api/discussions/{thread.id}/posts?page=1")
        data = resp.json()
        assert len(data["posts"]) == 25
        assert data["total_posts"] == 30
        assert data["page"] == 1
        assert data["total_pages"] == 2

        resp = c.get(f"/api/discussions/{thread.id}/posts?page=2")
        data = resp.json()
        assert len(data["posts"]) == 5
        assert data["page"] == 2

    def test_posts_ordered_chronologically(self, db):
        season = make_season(db)
        user = make_user(db)
        thread = make_thread(db, season)
        for i in range(3):
            db.add(DiscussionPost(thread_id=thread.id, user_id=user.id, content=f"Post {i}"))
        db.commit()

        c = anon_client()
        resp = c.get(f"/api/discussions/{thread.id}/posts")
        posts = resp.json()["posts"]
        assert posts[0]["content"] == "Post 0"
        assert posts[2]["content"] == "Post 2"

    def test_edit_own_post(self, db):
        season = make_season(db)
        user = make_user(db)
        thread = make_thread(db, season)
        post = DiscussionPost(thread_id=thread.id, user_id=user.id, content="Original")
        db.add(post)
        db.commit()
        db.refresh(post)

        c = client_for(user.id)
        resp = c.patch(
            f"/api/discussions/posts/{post.id}",
            json={"content": "Edited content"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == "Edited content"
        assert data["is_edited"] is True

    def test_cannot_edit_other_users_post(self, db):
        season = make_season(db)
        author = make_user(db, email="author@test.com")
        other = make_user(db, email="other@test.com")
        thread = make_thread(db, season)
        post = DiscussionPost(thread_id=thread.id, user_id=author.id, content="Original")
        db.add(post)
        db.commit()
        db.refresh(post)

        c = client_for(other.id)
        resp = c.patch(
            f"/api/discussions/posts/{post.id}",
            json={"content": "Hacked"},
        )
        assert resp.status_code == 403

    def test_admin_delete_post(self, db):
        season = make_season(db)
        user = make_user(db, email="user@test.com")
        admin = make_user(db, admin=True, email="admin@test.com")
        thread = make_thread(db, season)
        post = DiscussionPost(thread_id=thread.id, user_id=user.id, content="Delete me")
        db.add(post)
        db.commit()
        db.refresh(post)

        c = client_for(admin.id, is_admin=True)
        resp = c.delete(f"/api/admin/discussions/posts/{post.id}")
        assert resp.status_code == 200
        assert db.query(DiscussionPost).filter(DiscussionPost.id == post.id).first() is None

    def test_non_admin_cannot_delete(self, db):
        season = make_season(db)
        user = make_user(db)
        thread = make_thread(db, season)
        post = DiscussionPost(thread_id=thread.id, user_id=user.id, content="Keep me")
        db.add(post)
        db.commit()
        db.refresh(post)

        c = client_for(user.id)
        resp = c.delete(f"/api/admin/discussions/posts/{post.id}")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Reaction tests
# ---------------------------------------------------------------------------

class TestReactions:
    def test_toggle_reaction_on(self, db):
        season = make_season(db)
        user = make_user(db)
        thread = make_thread(db, season)
        post = DiscussionPost(thread_id=thread.id, user_id=user.id, content="React to me")
        db.add(post)
        db.commit()
        db.refresh(post)

        c = client_for(user.id)
        resp = c.post(
            f"/api/discussions/posts/{post.id}/reactions",
            json={"reaction_type": "like"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["reactions"]["like"] == 1
        assert data["user_reactions"] == ["like"]

    def test_toggle_reaction_off(self, db):
        season = make_season(db)
        user = make_user(db)
        thread = make_thread(db, season)
        post = DiscussionPost(thread_id=thread.id, user_id=user.id, content="React")
        db.add(post)
        db.commit()
        db.refresh(post)

        c = client_for(user.id)
        c.post(
            f"/api/discussions/posts/{post.id}/reactions",
            json={"reaction_type": "heart"},
        )
        resp = c.post(
            f"/api/discussions/posts/{post.id}/reactions",
            json={"reaction_type": "heart"},
        )
        data = resp.json()
        assert data["reactions"]["heart"] == 0
        assert data["user_reactions"] == []

    def test_multiple_reactions(self, db):
        season = make_season(db)
        user = make_user(db)
        thread = make_thread(db, season)
        post = DiscussionPost(thread_id=thread.id, user_id=user.id, content="React")
        db.add(post)
        db.commit()
        db.refresh(post)

        c = client_for(user.id)
        c.post(
            f"/api/discussions/posts/{post.id}/reactions",
            json={"reaction_type": "like"},
        )
        resp = c.post(
            f"/api/discussions/posts/{post.id}/reactions",
            json={"reaction_type": "heart"},
        )
        data = resp.json()
        assert data["reactions"]["like"] == 1
        assert data["reactions"]["heart"] == 1
        assert sorted(data["user_reactions"]) == ["heart", "like"]

    def test_invalid_reaction_type(self, db):
        season = make_season(db)
        user = make_user(db)
        thread = make_thread(db, season)
        post = DiscussionPost(thread_id=thread.id, user_id=user.id, content="React")
        db.add(post)
        db.commit()
        db.refresh(post)

        c = client_for(user.id)
        resp = c.post(
            f"/api/discussions/posts/{post.id}/reactions",
            json={"reaction_type": "angry"},
        )
        assert resp.status_code == 400

    def test_reaction_requires_auth(self, db):
        season = make_season(db)
        user = make_user(db)
        thread = make_thread(db, season)
        post = DiscussionPost(thread_id=thread.id, user_id=user.id, content="React")
        db.add(post)
        db.commit()
        db.refresh(post)

        c = anon_client()
        resp = c.post(
            f"/api/discussions/posts/{post.id}/reactions",
            json={"reaction_type": "like"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Display name tests
# ---------------------------------------------------------------------------

class TestDisplayName:
    def test_two_part_name(self):
        assert format_display_name("John Doe") == "John D."

    def test_single_name(self):
        assert format_display_name("Madonna") == "Madonna"

    def test_three_part_name(self):
        assert format_display_name("Mary Jane Watson") == "Mary W."

    def test_display_name_in_post_response(self, db):
        season = make_season(db)
        user = make_user(db, name="Jane Smith")
        thread = make_thread(db, season)
        c = client_for(user.id)
        resp = c.post(
            f"/api/discussions/{thread.id}/posts{sp(season)}",
            json={"content": "Hello"},
        )
        assert resp.json()["display_name"] == "Jane S."


# ---------------------------------------------------------------------------
# Episode count tests
# ---------------------------------------------------------------------------

class TestEpisodeCount:
    def test_set_episode_count(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        c = client_for(admin.id, is_admin=True)
        resp = c.patch(
            f"/api/admin/seasons/{season.id}/episode-count",
            json={"episode_count": 13},
        )
        assert resp.status_code == 200
        assert resp.json()["episode_count"] == 13

    def test_clear_episode_count(self, db):
        season = make_season(db, episode_count=13)
        admin = make_user(db, admin=True)
        c = client_for(admin.id, is_admin=True)
        resp = c.patch(
            f"/api/admin/seasons/{season.id}/episode-count",
            json={"episode_count": None},
        )
        assert resp.status_code == 200
        assert resp.json()["episode_count"] is None

    def test_episode_count_in_seasons_response(self, db):
        make_season(db, episode_count=14)
        c = anon_client()
        resp = c.get("/api/seasons")
        assert resp.json()[0]["episode_count"] == 14

    def test_episode_count_requires_admin(self, db):
        season = make_season(db)
        user = make_user(db)
        c = client_for(user.id)
        resp = c.patch(
            f"/api/admin/seasons/{season.id}/episode-count",
            json={"episode_count": 13},
        )
        assert resp.status_code == 403
