"""Tests for the bonus questions feature.

Covers question creation, answer submission (with deadline enforcement),
wager validation, all_answers visibility gating, grading, score/leaderboard
integration, cascade deletes, and season scoping.
"""

import json
import base64
from datetime import datetime, timezone, timedelta
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import Season, User, Contestant, Ranking, BonusQuestion, BonusAnswer
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
from starlette.middleware.sessions import SessionMiddleware
test_app.add_middleware(SessionMiddleware, secret_key=TEST_SECRET)
test_app.include_router(router)
test_app.dependency_overrides[get_db] = override_get_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_session_cookie(data: dict) -> str:
    payload = base64.b64encode(json.dumps(data).encode())
    return TimestampSigner(TEST_SECRET).sign(payload).decode()


def client_for(user_id: int, is_admin: bool = False) -> TestClient:
    c = TestClient(test_app, raise_server_exceptions=True)
    c.cookies.set("session", make_session_cookie({"user_id": user_id, "is_admin": is_admin}))
    return c


def anon_client() -> TestClient:
    return TestClient(test_app, raise_server_exceptions=True)


def sp(season: Season) -> str:
    return f"?season={season.id}"


def future_utc(hours: int = 24) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=hours)


def past_utc(hours: int = 1) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)


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


def make_season(db, number: int = 1, active: bool = True) -> Season:
    s = Season(number=number, name=f"Season {number}", is_active=active)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def make_user(db, admin: bool = False, email: Optional[str] = None) -> User:
    if email is None:
        import random
        email = f"user{random.randint(1000, 9999)}@test.com"
    u = User(email=email, name=email.split("@")[0], is_admin=admin)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def make_standard_question(db, season: Season, deadline: Optional[datetime] = None,
                            points_value: int = 10, partial: int = 5) -> BonusQuestion:
    q = BonusQuestion(
        season_id=season.id,
        question_text="Who is the most athletic player?",
        question_type="standard",
        deadline_utc=deadline or future_utc(),
        points_value=points_value,
        partial_points_value=partial,
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


def make_wager_question(db, season: Season, deadline: Optional[datetime] = None,
                        max_wager: int = 10) -> BonusQuestion:
    q = BonusQuestion(
        season_id=season.id,
        question_text="Pick the winner.",
        question_type="wager",
        deadline_utc=deadline or future_utc(),
        max_wager=max_wager,
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


def make_ranking(db, user: User, season: Season, n: int = 3):
    """Create n contestants and a full set of rankings so scores endpoint works."""
    contestants = []
    for i in range(n):
        c = Contestant(season_id=season.id, name=f"Player {i+1}", tribe="A")
        db.add(c)
        db.flush()
        contestants.append(c)
    db.commit()
    for i, c in enumerate(contestants):
        r = Ranking(
            user_id=user.id,
            season_id=season.id,
            contestant_id=c.id,
            rank=i + 1,
            scoring_eligible=True,
        )
        db.add(r)
    db.commit()


# ---------------------------------------------------------------------------
# Admin creation tests
# ---------------------------------------------------------------------------

class TestBonusQuestionCreate:
    def test_admin_can_create_standard_question(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions{sp(season)}", json={
            "question_text": "Who is the most athletic?",
            "question_type": "standard",
            "deadline_utc": future_utc(48).isoformat() + "Z",
            "points_value": 10,
            "partial_points_value": 5,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["question_type"] == "standard"
        assert data["points_value"] == 10
        assert data["partial_points_value"] == 5

    def test_admin_can_create_wager_question(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions{sp(season)}", json={
            "question_text": "Pick the winner.",
            "question_type": "wager",
            "deadline_utc": future_utc(48).isoformat() + "Z",
            "max_wager": 10,
        })
        assert resp.status_code == 200
        assert resp.json()["max_wager"] == 10

    def test_non_admin_cannot_create_question(self, db):
        season = make_season(db)
        user = make_user(db)
        c = client_for(user.id, is_admin=False)

        resp = c.post(f"/api/admin/bonus-questions{sp(season)}", json={
            "question_text": "Q",
            "question_type": "standard",
            "deadline_utc": future_utc().isoformat() + "Z",
            "points_value": 5,
        })
        assert resp.status_code == 403

    def test_invalid_type_rejected(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions{sp(season)}", json={
            "question_text": "Q",
            "question_type": "invalid",
            "deadline_utc": future_utc().isoformat() + "Z",
        })
        assert resp.status_code == 400

    def test_standard_requires_points_value(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions{sp(season)}", json={
            "question_text": "Q",
            "question_type": "standard",
            "deadline_utc": future_utc().isoformat() + "Z",
            # missing points_value
        })
        assert resp.status_code == 400

    def test_wager_requires_max_wager(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions{sp(season)}", json={
            "question_text": "Q",
            "question_type": "wager",
            "deadline_utc": future_utc().isoformat() + "Z",
            # missing max_wager
        })
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Answer submission tests
# ---------------------------------------------------------------------------

class TestBonusAnswerSubmit:
    def test_submit_answer_before_deadline(self, db):
        season = make_season(db)
        user = make_user(db)
        q = make_standard_question(db, season)
        c = client_for(user.id)

        resp = c.post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={
            "answer_text": "Joe",
        })
        assert resp.status_code == 200
        assert resp.json()["answer_text"] == "joe"  # string type normalises to lowercase

    def test_submit_answer_after_deadline_rejected(self, db):
        season = make_season(db)
        user = make_user(db)
        q = make_standard_question(db, season, deadline=past_utc())
        c = client_for(user.id)

        resp = c.post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={
            "answer_text": "Joe",
        })
        assert resp.status_code == 400
        assert "deadline" in resp.json()["detail"].lower()

    def test_update_answer_before_deadline(self, db):
        season = make_season(db)
        user = make_user(db)
        q = make_standard_question(db, season)
        c = client_for(user.id)

        c.post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={"answer_text": "Joe"})
        resp = c.post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={"answer_text": "Maria"})
        assert resp.status_code == 200
        assert resp.json()["answer_text"] == "maria"  # string type normalises to lowercase

        # Confirm only one record in DB
        session = TestingSession()
        count = session.query(BonusAnswer).filter(BonusAnswer.question_id == q.id).count()
        session.close()
        assert count == 1

    def test_unauthenticated_cannot_submit(self, db):
        season = make_season(db)
        q = make_standard_question(db, season)
        resp = anon_client().post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={"answer_text": "Joe"})
        assert resp.status_code == 401

    def test_empty_answer_rejected(self, db):
        season = make_season(db)
        user = make_user(db)
        q = make_standard_question(db, season)
        c = client_for(user.id)

        resp = c.post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={"answer_text": "  "})
        assert resp.status_code == 400


class TestWagerValidation:
    def test_wager_required_for_wager_question(self, db):
        season = make_season(db)
        user = make_user(db)
        q = make_wager_question(db, season)
        c = client_for(user.id)

        resp = c.post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={"answer_text": "Joe"})
        assert resp.status_code == 400
        assert "wager" in resp.json()["detail"].lower()

    def test_wager_exceeds_max_rejected(self, db):
        season = make_season(db)
        user = make_user(db)
        q = make_wager_question(db, season, max_wager=10)
        c = client_for(user.id)

        resp = c.post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={
            "answer_text": "Joe", "wager": 11,
        })
        assert resp.status_code == 400

    def test_wager_below_one_rejected(self, db):
        season = make_season(db)
        user = make_user(db)
        q = make_wager_question(db, season, max_wager=10)
        c = client_for(user.id)

        resp = c.post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={
            "answer_text": "Joe", "wager": 0,
        })
        assert resp.status_code == 400

    def test_valid_wager_accepted(self, db):
        season = make_season(db)
        user = make_user(db)
        q = make_wager_question(db, season, max_wager=10)
        c = client_for(user.id)

        resp = c.post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={
            "answer_text": "Joe", "wager": 7,
        })
        assert resp.status_code == 200
        assert resp.json()["wager"] == 7


# ---------------------------------------------------------------------------
# all_answers visibility gating
# ---------------------------------------------------------------------------

class TestAnswerVisibility:
    def test_all_answers_empty_before_deadline(self, db):
        season = make_season(db)
        user = make_user(db)
        q = make_standard_question(db, season, deadline=future_utc(48))
        c = client_for(user.id)
        # Submit an answer
        c.post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={"answer_text": "Joe"})

        resp = client_for(make_user(db).id).get(f"/api/bonus-questions{sp(season)}")
        assert resp.status_code == 200
        questions = resp.json()
        assert questions[0]["all_answers"] == []
        assert questions[0]["is_past_deadline"] is False

    def test_all_answers_visible_after_deadline(self, db):
        season = make_season(db)
        user = make_user(db)
        q = make_standard_question(db, season, deadline=past_utc())
        # Inject an answer directly into DB (deadline already passed)
        session = TestingSession()
        ba = BonusAnswer(question_id=q.id, user_id=user.id, answer_text="Joe")
        session.add(ba)
        session.commit()
        session.close()

        c = client_for(user.id)
        resp = c.get(f"/api/bonus-questions{sp(season)}")
        assert resp.status_code == 200
        questions = resp.json()
        assert questions[0]["is_past_deadline"] is True
        assert len(questions[0]["all_answers"]) == 1
        assert questions[0]["all_answers"][0]["answer_text"] == "Joe"

    def test_admin_cannot_see_answers_before_deadline(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        user = make_user(db)
        q = make_standard_question(db, season, deadline=future_utc(48))
        client_for(user.id).post(
            f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={"answer_text": "Joe"}
        )

        resp = client_for(admin.id, is_admin=True).get(f"/api/bonus-questions{sp(season)}")
        assert resp.json()[0]["all_answers"] == []


# ---------------------------------------------------------------------------
# Grading tests
# ---------------------------------------------------------------------------

class TestGrading:
    def _setup(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        user = make_user(db)
        return season, admin, user

    def _add_answer(self, db, question_id, user_id, answer_text="Joe", wager=None):
        session = TestingSession()
        ba = BonusAnswer(question_id=question_id, user_id=user_id, answer_text=answer_text, wager=wager)
        session.add(ba)
        session.commit()
        session.close()

    def test_grade_standard_correct(self, db):
        season, admin, user = self._setup(db)
        q = make_standard_question(db, season, points_value=10, partial=5)
        self._add_answer(db, q.id, user.id)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions/{q.id}/grade", json={
            "user_id": user.id, "outcome": "correct",
        })
        assert resp.status_code == 200
        assert resp.json()["points_earned"] == 10

    def test_grade_standard_partial(self, db):
        season, admin, user = self._setup(db)
        q = make_standard_question(db, season, points_value=10, partial=5)
        self._add_answer(db, q.id, user.id)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions/{q.id}/grade", json={
            "user_id": user.id, "outcome": "partial",
        })
        assert resp.status_code == 200
        assert resp.json()["points_earned"] == 5

    def test_grade_standard_incorrect(self, db):
        season, admin, user = self._setup(db)
        q = make_standard_question(db, season, points_value=10, partial=5)
        self._add_answer(db, q.id, user.id)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions/{q.id}/grade", json={
            "user_id": user.id, "outcome": "incorrect",
        })
        assert resp.status_code == 200
        assert resp.json()["points_earned"] == 0

    def test_grade_wager_correct(self, db):
        season, admin, user = self._setup(db)
        q = make_wager_question(db, season, max_wager=10)
        self._add_answer(db, q.id, user.id, wager=7)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions/{q.id}/grade", json={
            "user_id": user.id, "outcome": "correct",
        })
        assert resp.json()["points_earned"] == 7

    def test_grade_wager_partial(self, db):
        season, admin, user = self._setup(db)
        q = make_wager_question(db, season, max_wager=10)
        self._add_answer(db, q.id, user.id, wager=7)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions/{q.id}/grade", json={
            "user_id": user.id, "outcome": "partial",
        })
        assert resp.json()["points_earned"] == 0

    def test_grade_wager_incorrect(self, db):
        season, admin, user = self._setup(db)
        q = make_wager_question(db, season, max_wager=10)
        self._add_answer(db, q.id, user.id, wager=7)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions/{q.id}/grade", json={
            "user_id": user.id, "outcome": "incorrect",
        })
        assert resp.json()["points_earned"] == -7

    def test_invalid_outcome_rejected(self, db):
        season, admin, user = self._setup(db)
        q = make_standard_question(db, season)
        self._add_answer(db, q.id, user.id)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions/{q.id}/grade", json={
            "user_id": user.id, "outcome": "kinda-right",
        })
        assert resp.status_code == 400

    def test_non_admin_cannot_grade(self, db):
        season, _, user = self._setup(db)
        q = make_standard_question(db, season)
        self._add_answer(db, q.id, user.id)

        resp = client_for(user.id).post(f"/api/admin/bonus-questions/{q.id}/grade", json={
            "user_id": user.id, "outcome": "correct",
        })
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Score and leaderboard integration
# ---------------------------------------------------------------------------

class TestScoreIntegration:
    def test_bonus_points_appear_in_scores(self, db):
        season = make_season(db)
        user = make_user(db)
        make_ranking(db, user, season, n=3)
        q = make_standard_question(db, season, points_value=10, partial=5)

        # Grade the answer
        session = TestingSession()
        ba = BonusAnswer(question_id=q.id, user_id=user.id, answer_text="Joe",
                         outcome="correct", points_earned=10)
        session.add(ba)
        session.commit()
        session.close()

        c = client_for(user.id)
        resp = c.get(f"/api/scores{sp(season)}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["bonus_total"] == 10
        assert data["ranking_score"] == data["total_score"] - 10
        assert len(data["bonus_questions"]) == 1
        assert data["bonus_questions"][0]["points_earned"] == 10

    def test_negative_bonus_points_reduce_total(self, db):
        season = make_season(db)
        user = make_user(db)
        make_ranking(db, user, season, n=3)
        q = make_wager_question(db, season, max_wager=10)

        session = TestingSession()
        ba = BonusAnswer(question_id=q.id, user_id=user.id, answer_text="Joe",
                         wager=8, outcome="incorrect", points_earned=-8)
        session.add(ba)
        session.commit()
        session.close()

        resp = client_for(user.id).get(f"/api/scores{sp(season)}")
        data = resp.json()
        assert data["bonus_total"] == -8
        assert data["total_score"] == data["ranking_score"] - 8

    def test_ungraded_bonus_not_counted(self, db):
        season = make_season(db)
        user = make_user(db)
        make_ranking(db, user, season, n=3)
        q = make_standard_question(db, season)

        session = TestingSession()
        ba = BonusAnswer(question_id=q.id, user_id=user.id, answer_text="Joe")
        session.add(ba)
        session.commit()
        session.close()

        resp = client_for(user.id).get(f"/api/scores{sp(season)}")
        data = resp.json()
        assert data["bonus_total"] == 0
        assert len(data["bonus_questions"]) == 0

    def test_bonus_points_in_leaderboard(self, db):
        season = make_season(db)
        user = make_user(db)
        make_ranking(db, user, season, n=3)
        q = make_standard_question(db, season, points_value=10)

        session = TestingSession()
        ba = BonusAnswer(question_id=q.id, user_id=user.id, answer_text="Joe",
                         outcome="correct", points_earned=10)
        session.add(ba)
        session.commit()
        session.close()

        resp = client_for(user.id).get(f"/api/leaderboard{sp(season)}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        entry = data[0]
        assert entry["bonus_total"] == 10
        assert entry["total_score"] == entry["ranking_score"] + 10


# ---------------------------------------------------------------------------
# Delete cascade
# ---------------------------------------------------------------------------

class TestDeleteCascade:
    def test_delete_question_removes_answers(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        user = make_user(db)
        q = make_standard_question(db, season)

        session = TestingSession()
        ba = BonusAnswer(question_id=q.id, user_id=user.id, answer_text="Joe")
        session.add(ba)
        session.commit()
        session.close()

        c = client_for(admin.id, is_admin=True)
        resp = c.delete(f"/api/admin/bonus-questions/{q.id}")
        assert resp.status_code == 200

        session = TestingSession()
        count = session.query(BonusAnswer).filter(BonusAnswer.question_id == q.id).count()
        session.close()
        assert count == 0


# ---------------------------------------------------------------------------
# Season scoping
# ---------------------------------------------------------------------------

class TestSeasonScoping:
    def test_questions_scoped_to_season(self, db):
        season1 = make_season(db, number=1, active=True)
        season2 = make_season(db, number=2, active=False)
        make_standard_question(db, season1)
        make_standard_question(db, season2)

        resp = anon_client().get(f"/api/bonus-questions{sp(season1)}")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        resp = anon_client().get(f"/api/bonus-questions{sp(season2)}")
        assert len(resp.json()) == 1

    def test_cannot_create_question_for_inactive_season(self, db):
        inactive = make_season(db, number=99, active=False)
        admin = make_user(db, admin=True)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions{sp(inactive)}", json={
            "question_text": "Q",
            "question_type": "standard",
            "deadline_utc": future_utc().isoformat() + "Z",
            "points_value": 5,
        })
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Answer type tests
# ---------------------------------------------------------------------------

def make_contestant(db, season: Season, name: str = "Joe Survivor") -> Contestant:
    c = Contestant(season_id=season.id, name=name, tribe="A")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def make_question_with_answer_type(db, season: Season, answer_type: str) -> BonusQuestion:
    q = BonusQuestion(
        season_id=season.id,
        question_text="Test question",
        question_type="standard",
        answer_type=answer_type,
        deadline_utc=future_utc(),
        points_value=10,
        partial_points_value=5,
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


class TestAnswerTypes:
    # --- contestant ---

    def test_contestant_answer_valid_name_accepted(self, db):
        season = make_season(db)
        user = make_user(db)
        make_contestant(db, season, "Maria Shrime Gonzalez")
        q = make_question_with_answer_type(db, season, "contestant")

        resp = client_for(user.id).post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={
            "answer_text": "Maria Shrime Gonzalez",
        })
        assert resp.status_code == 200
        assert resp.json()["answer_text"] == "Maria Shrime Gonzalez"

    def test_contestant_answer_invalid_name_rejected(self, db):
        season = make_season(db)
        user = make_user(db)
        make_contestant(db, season, "Maria Shrime Gonzalez")
        q = make_question_with_answer_type(db, season, "contestant")

        resp = client_for(user.id).post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={
            "answer_text": "Nobody Real",
        })
        assert resp.status_code == 400
        assert "contestant" in resp.json()["detail"].lower()

    def test_contestant_answer_type_returned_in_question(self, db):
        season = make_season(db)
        q = make_question_with_answer_type(db, season, "contestant")

        resp = anon_client().get(f"/api/bonus-questions{sp(season)}")
        assert resp.json()[0]["answer_type"] == "contestant"

    def test_admin_create_sets_answer_type(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions{sp(season)}", json={
            "question_text": "Pick the winner",
            "question_type": "wager",
            "answer_type": "contestant",
            "deadline_utc": future_utc().isoformat() + "Z",
            "max_wager": 10,
        })
        assert resp.status_code == 200
        assert resp.json()["answer_type"] == "contestant"

    def test_invalid_answer_type_rejected_on_create(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions{sp(season)}", json={
            "question_text": "Q",
            "question_type": "standard",
            "answer_type": "emoji",
            "deadline_utc": future_utc().isoformat() + "Z",
            "points_value": 5,
        })
        assert resp.status_code == 400

    # --- integer ---

    def test_integer_answer_valid_accepted(self, db):
        season = make_season(db)
        user = make_user(db)
        q = make_question_with_answer_type(db, season, "integer")

        resp = client_for(user.id).post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={
            "answer_text": "42",
        })
        assert resp.status_code == 200
        assert resp.json()["answer_text"] == "42"

    def test_integer_answer_non_integer_rejected(self, db):
        season = make_season(db)
        user = make_user(db)
        q = make_question_with_answer_type(db, season, "integer")

        resp = client_for(user.id).post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={
            "answer_text": "forty-two",
        })
        assert resp.status_code == 400

    def test_integer_answer_float_rejected(self, db):
        season = make_season(db)
        user = make_user(db)
        q = make_question_with_answer_type(db, season, "integer")

        resp = client_for(user.id).post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={
            "answer_text": "3.14",
        })
        assert resp.status_code == 400

    # --- string (case normalisation) ---

    def test_string_answer_normalised_to_lowercase(self, db):
        season = make_season(db)
        user = make_user(db)
        q = make_question_with_answer_type(db, season, "string")

        resp = client_for(user.id).post(f"/api/bonus-questions/{q.id}/answer{sp(season)}", json={
            "answer_text": "Boston Rob",
        })
        assert resp.status_code == 200
        assert resp.json()["answer_text"] == "boston rob"

    def test_default_answer_type_is_string(self, db):
        season = make_season(db)
        admin = make_user(db, admin=True)
        c = client_for(admin.id, is_admin=True)

        resp = c.post(f"/api/admin/bonus-questions{sp(season)}", json={
            "question_text": "Q",
            "question_type": "standard",
            "deadline_utc": future_utc().isoformat() + "Z",
            "points_value": 5,
            # no answer_type — should default to "string"
        })
        assert resp.status_code == 200
        assert resp.json()["answer_type"] == "string"
