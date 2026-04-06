from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from datetime import datetime, timezone

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    picture = Column(String, nullable=True)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    rankings = relationship("Ranking", back_populates="user", cascade="all, delete-orphan")
    bonus_answers = relationship("BonusAnswer", back_populates="user", cascade="all, delete-orphan")


class Season(Base):
    __tablename__ = "seasons"

    id = Column(Integer, primary_key=True, index=True)
    number = Column(Integer, unique=True, nullable=False)
    name = Column(String, nullable=False)
    is_active = Column(Boolean, default=False)
    episode_count = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    contestants = relationship("Contestant", back_populates="season", cascade="all, delete-orphan")
    tribe_configs = relationship("TribeConfig", back_populates="season", cascade="all, delete-orphan")
    rankings = relationship("Ranking", back_populates="season", cascade="all, delete-orphan")
    episode_threads = relationship("EpisodeThread", back_populates="season", cascade="all, delete-orphan")
    bonus_questions = relationship("BonusQuestion", back_populates="season", cascade="all, delete-orphan")


class Contestant(Base):
    __tablename__ = "contestants"

    id = Column(Integer, primary_key=True, index=True)
    season_id = Column(Integer, ForeignKey("seasons.id"), nullable=False)
    name = Column(String, nullable=False)
    tribe = Column(String, nullable=True)
    image_url = Column(String, nullable=True)
    elimination_order = Column(Integer, nullable=True)  # null = still in game, 1 = first eliminated
    is_removed = Column(Boolean, default=False)  # removed for medical/other reasons, no points awarded

    season = relationship("Season", back_populates="contestants")
    rankings = relationship("Ranking", back_populates="contestant", cascade="all, delete-orphan")


class TribeConfig(Base):
    __tablename__ = "tribe_configs"

    id = Column(Integer, primary_key=True, index=True)
    season_id = Column(Integer, ForeignKey("seasons.id"), nullable=False)
    name = Column(String, nullable=False)
    color = Column(String, nullable=False)  # hex like #9b59b6
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    season = relationship("Season", back_populates="tribe_configs")

    __table_args__ = (
        UniqueConstraint("season_id", "name", name="uq_season_tribe_name"),
    )


class Ranking(Base):
    __tablename__ = "rankings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    season_id = Column(Integer, ForeignKey("seasons.id"), nullable=False)
    contestant_id = Column(Integer, ForeignKey("contestants.id"), nullable=False)
    rank = Column(Integer, nullable=False)  # 1 = predicted winner, N = predicted first out
    locked = Column(Boolean, default=False)
    scoring_eligible = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="rankings")
    season = relationship("Season", back_populates="rankings")
    contestant = relationship("Contestant", back_populates="rankings")

    __table_args__ = (
        UniqueConstraint("user_id", "contestant_id", name="uq_user_contestant"),
        UniqueConstraint("user_id", "season_id", "rank", name="uq_user_season_rank"),
    )


class EpisodeThread(Base):
    __tablename__ = "episode_threads"

    id = Column(Integer, primary_key=True, index=True)
    season_id = Column(Integer, ForeignKey("seasons.id"), nullable=False)
    episode_number = Column(Integer, nullable=False)
    title = Column(String, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    season = relationship("Season", back_populates="episode_threads")
    posts = relationship("DiscussionPost", back_populates="thread", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("season_id", "episode_number", name="uq_season_episode"),
    )


class DiscussionPost(Base):
    __tablename__ = "discussion_posts"

    id = Column(Integer, primary_key=True, index=True)
    thread_id = Column(Integer, ForeignKey("episode_threads.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(String, nullable=False)
    is_edited = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    thread = relationship("EpisodeThread", back_populates="posts")
    user = relationship("User")
    reactions = relationship("PostReaction", back_populates="post", cascade="all, delete-orphan")


class PostReaction(Base):
    __tablename__ = "post_reactions"

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("discussion_posts.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    reaction_type = Column(String, nullable=False)  # "like", "heart", "sad"

    post = relationship("DiscussionPost", back_populates="reactions")
    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("post_id", "user_id", "reaction_type", name="uq_post_user_reaction_type"),
    )


class RankingAuditSubmission(Base):
    __tablename__ = "ranking_audit_submissions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    season_id = Column(Integer, ForeignKey("seasons.id"), nullable=False)
    session_user_email = Column(String, nullable=True)
    session_user_name = Column(String, nullable=True)
    client_ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
    contestant_count = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    entries = relationship("RankingAuditEntry", back_populates="submission", cascade="all, delete-orphan")
    user = relationship("User")
    season = relationship("Season")


class RankingAuditEntry(Base):
    __tablename__ = "ranking_audit_entries"

    id = Column(Integer, primary_key=True, index=True)
    submission_id = Column(Integer, ForeignKey("ranking_audit_submissions.id"), nullable=False)
    contestant_id = Column(Integer, ForeignKey("contestants.id"), nullable=False)
    contestant_name = Column(String, nullable=False)
    rank = Column(Integer, nullable=False)

    submission = relationship("RankingAuditSubmission", back_populates="entries")


class BonusQuestion(Base):
    __tablename__ = "bonus_questions"

    id = Column(Integer, primary_key=True, index=True)
    season_id = Column(Integer, ForeignKey("seasons.id"), nullable=False)
    question_text = Column(String, nullable=False)
    question_type = Column(String, nullable=False)   # "standard" or "wager"
    answer_type = Column(String, nullable=True)      # "contestant", "integer", or "string" (default)
    deadline_utc = Column(DateTime, nullable=False)  # stored as UTC
    # Standard scoring fields
    points_value = Column(Integer, nullable=True)          # full credit
    partial_points_value = Column(Integer, nullable=True)  # partial credit (default 0)
    # Wager scoring fields
    max_wager = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    answers = relationship("BonusAnswer", back_populates="question", cascade="all, delete-orphan")
    season = relationship("Season", back_populates="bonus_questions")


class BonusAnswer(Base):
    __tablename__ = "bonus_answers"

    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(Integer, ForeignKey("bonus_questions.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    answer_text = Column(String, nullable=False)
    wager = Column(Integer, nullable=True)          # wager questions only
    outcome = Column(String, nullable=True)         # NULL until graded: "correct"/"partial"/"incorrect"
    points_earned = Column(Integer, nullable=True)  # NULL until graded
    submitted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    question = relationship("BonusQuestion", back_populates="answers")
    user = relationship("User", back_populates="bonus_answers")

    __table_args__ = (
        UniqueConstraint("question_id", "user_id", name="uq_bonus_question_user"),
    )
