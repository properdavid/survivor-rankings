"""API routes for the Survivor ranking app."""

import io
import logging
import os
import shutil
from datetime import datetime, timezone
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session
from pydantic import BaseModel
import httpx
from PIL import Image

from app.database import engine, get_db, get_db_path
import re
from typing import Optional
from sqlalchemy import func
from app.models import User, Season, Contestant, Ranking, TribeConfig, EpisodeThread, DiscussionPost, PostReaction, RankingAuditSubmission, RankingAuditEntry, BonusQuestion, BonusAnswer
from app.scoring import calculate_total_score
from app.email import send_rankings_email, send_broadcast_email, is_email_configured

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["api"])


# --- Request models ---

class SeasonCreate(BaseModel):
    number: int
    name: str


class ContestantTribeUpdate(BaseModel):
    tribe: str


class TribeCreate(BaseModel):
    name: str
    color: str


class TribeColorUpdate(BaseModel):
    color: str


class RemoveContestantRequest(BaseModel):
    contestant_id: int
    elimination_order: int


class ResetContestantRequest(BaseModel):
    contestant_id: int


class EliminationUpdate(BaseModel):
    contestant_id: int
    elimination_order: int


class RankingItem(BaseModel):
    contestant_id: int
    rank: int


class RankingSubmission(BaseModel):
    rankings: list[RankingItem]


class RoleUpdate(BaseModel):
    is_admin: bool


class EpisodeThreadCreate(BaseModel):
    episode_number: int
    title: str


class EpisodeThreadUpdate(BaseModel):
    title: str


class PostCreate(BaseModel):
    content: str


class PostUpdate(BaseModel):
    content: str


class ReactionToggle(BaseModel):
    reaction_type: str


class EpisodeCountUpdate(BaseModel):
    episode_count: Optional[int] = None


class BonusQuestionCreate(BaseModel):
    question_text: str
    question_type: str          # "standard" or "wager"
    answer_type: str = "string" # "contestant", "integer", or "string"
    deadline_utc: str           # ISO 8601 UTC string from frontend
    points_value: Optional[int] = None
    partial_points_value: Optional[int] = 0
    max_wager: Optional[int] = None


class BonusQuestionUpdate(BaseModel):
    question_text: Optional[str] = None
    answer_type: Optional[str] = None
    deadline_utc: Optional[str] = None
    points_value: Optional[int] = None
    partial_points_value: Optional[int] = None
    max_wager: Optional[int] = None


class BonusAnswerSubmit(BaseModel):
    answer_text: str
    wager: Optional[int] = None  # required for wager questions


class BonusGrade(BaseModel):
    user_id: int
    outcome: str                # "correct", "partial", "incorrect"


class BroadcastEmailRequest(BaseModel):
    user_ids: list[int]
    subject: str
    body_html: str
    body_text: str


# In-memory cache for cropped images: url -> jpeg bytes (insertion-order LRU eviction)
_IMAGE_CACHE_MAX = 500
_image_cache: dict[str, bytes] = {}


def _crop_to_face(data: bytes, output_size: int = 200) -> bytes:
    """Crop image to a face-focused square and resize."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size

    if h > w * 1.1:
        side = int(w * 0.70)
        left = (w - side) // 2
        top = int(h * 0.02)
        img = img.crop((left, top, left + side, top + side))
    else:
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))

    img = img.resize((output_size, output_size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()


@router.get("/image-proxy")
async def image_proxy(url: str):
    """Fetch a remote image, crop to face region, and return as JPEG."""
    if url in _image_cache:
        return Response(content=_image_cache[url], media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            cropped = _crop_to_face(resp.content)
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to fetch or process image")

    if len(_image_cache) >= _IMAGE_CACHE_MAX:
        _image_cache.pop(next(iter(_image_cache)))
    _image_cache[url] = cropped
    return Response(content=cropped, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


# --- Auth helpers ---

def get_current_user_id(request: Request) -> int:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


def require_admin(request: Request) -> int:
    user_id = get_current_user_id(request)
    if not request.session.get("is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id


def get_client_ip(request: Request) -> str:
    """Extract real client IP: CF-Connecting-IP > X-Forwarded-For > request.client."""
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


# --- Season resolution ---

def get_season(request: Request, db: Session = Depends(get_db)) -> Season:
    """Resolve the season from ?season= query param, or default to the active season."""
    season_id = request.query_params.get("season")
    if season_id:
        season = db.query(Season).filter(Season.id == int(season_id)).first()
    else:
        season = db.query(Season).filter(Season.is_active == True).first()
    if not season:
        raise HTTPException(status_code=404, detail="Season not found")
    return season


def require_active_season(season: Season) -> Season:
    """Raise 400 if the season is not the active one (read-only for past seasons)."""
    if not season.is_active:
        raise HTTPException(status_code=400, detail="This season is not active. Past seasons are read-only.")
    return season


# --- Season endpoints ---

@router.get("/seasons")
def get_seasons(db: Session = Depends(get_db)):
    seasons = db.query(Season).order_by(Season.number.desc()).all()
    return [
        {"id": s.id, "number": s.number, "name": s.name, "is_active": s.is_active, "episode_count": s.episode_count}
        for s in seasons
    ]


@router.post("/admin/seasons")
def create_season(data: SeasonCreate, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    existing = db.query(Season).filter(Season.number == data.number).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Season {data.number} already exists")
    season = Season(number=data.number, name=data.name.strip(), is_active=False)
    db.add(season)
    db.commit()
    db.refresh(season)
    return {"id": season.id, "number": season.number, "name": season.name, "is_active": season.is_active}


@router.post("/admin/seasons/{season_id}/activate")
def activate_season(season_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    season = db.query(Season).filter(Season.id == season_id).first()
    if not season:
        raise HTTPException(status_code=404, detail="Season not found")
    # Deactivate all others, activate this one
    db.query(Season).update({"is_active": False})
    season.is_active = True
    db.commit()
    return {"message": f"{season.name} is now active", "id": season.id}


# --- Contestant endpoints ---

@router.get("/contestants")
def get_contestants(
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    contestants = (
        db.query(Contestant)
        .filter(Contestant.season_id == season.id)
        .order_by(Contestant.id)
        .all()
    )
    total = len(contestants)
    return [
        {
            "id": c.id,
            "name": c.name,
            "tribe": c.tribe,
            "image_url": c.image_url,
            "elimination_order": c.elimination_order,
            "is_winner": c.elimination_order is not None and c.elimination_order == total,
            "is_removed": c.is_removed,
        }
        for c in contestants
    ]


@router.post("/admin/contestants/{contestant_id}/tribe")
def update_contestant_tribe(
    contestant_id: int,
    data: ContestantTribeUpdate,
    request: Request,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    require_admin(request)
    require_active_season(season)
    tribe_name = data.tribe.strip()
    valid = db.query(TribeConfig).filter(
        TribeConfig.season_id == season.id, TribeConfig.name == tribe_name
    ).first()
    if not valid:
        available = sorted(
            t.name for t in db.query(TribeConfig).filter(TribeConfig.season_id == season.id).all()
        )
        raise HTTPException(status_code=400, detail=f"Tribe must be one of: {', '.join(available)}")
    contestant = db.query(Contestant).filter(Contestant.id == contestant_id).first()
    if not contestant:
        raise HTTPException(status_code=404, detail="Contestant not found")
    contestant.tribe = tribe_name
    db.commit()
    return {"message": f"{contestant.name} moved to {tribe_name}"}


# --- Tribe configuration endpoints ---

@router.get("/tribes")
def get_tribes(season: Season = Depends(get_season), db: Session = Depends(get_db)):
    tribes = (
        db.query(TribeConfig)
        .filter(TribeConfig.season_id == season.id)
        .order_by(TribeConfig.name)
        .all()
    )
    return [{"id": t.id, "name": t.name, "color": t.color} for t in tribes]


@router.post("/admin/tribes")
def create_tribe(
    data: TribeCreate,
    request: Request,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    require_admin(request)
    require_active_season(season)
    name = data.name.strip()
    color = data.color.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Tribe name is required")
    if not re.match(r'^#[0-9a-fA-F]{6}$', color):
        raise HTTPException(status_code=400, detail="Color must be a 6-digit hex value like #ff0000")
    existing = db.query(TribeConfig).filter(
        TribeConfig.season_id == season.id, TribeConfig.name == name
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Tribe '{name}' already exists in this season")
    tribe = TribeConfig(season_id=season.id, name=name, color=color)
    db.add(tribe)
    db.commit()
    db.refresh(tribe)
    return {"id": tribe.id, "name": tribe.name, "color": tribe.color, "message": f"Tribe '{tribe.name}' created"}


@router.patch("/admin/tribes/{tribe_id}")
def update_tribe(tribe_id: int, data: TribeColorUpdate, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    tribe = db.query(TribeConfig).filter(TribeConfig.id == tribe_id).first()
    if not tribe:
        raise HTTPException(status_code=404, detail="Tribe not found")
    color = data.color.strip()
    if not re.match(r'^#[0-9a-fA-F]{6}$', color):
        raise HTTPException(status_code=400, detail="Color must be a 6-digit hex value like #ff0000")
    tribe.color = color
    db.commit()
    return {"message": f"'{tribe.name}' color updated", "id": tribe.id, "name": tribe.name, "color": tribe.color}


@router.delete("/admin/tribes/{tribe_id}")
def delete_tribe(tribe_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    tribe = db.query(TribeConfig).filter(TribeConfig.id == tribe_id).first()
    if not tribe:
        raise HTTPException(status_code=404, detail="Tribe not found")
    assigned = db.query(Contestant).filter(
        Contestant.season_id == tribe.season_id, Contestant.tribe == tribe.name
    ).count()
    if assigned > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot remove '{tribe.name}': {assigned} contestant(s) still assigned. Reassign them first."
        )
    db.delete(tribe)
    db.commit()
    return {"message": f"Tribe '{tribe.name}' removed"}


# --- Elimination endpoints ---

@router.post("/admin/eliminate")
def update_elimination(
    update: EliminationUpdate,
    request: Request,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    require_admin(request)
    require_active_season(season)
    contestant = db.query(Contestant).filter(Contestant.id == update.contestant_id).first()
    if not contestant:
        raise HTTPException(status_code=404, detail="Contestant not found")

    contestant.elimination_order = update.elimination_order
    db.commit()

    # Lock this season's rankings — at least one elimination has now been recorded.
    db.query(Ranking).filter(Ranking.season_id == season.id).update({"locked": True})
    db.commit()

    return {"message": f"{contestant.name} marked as elimination #{update.elimination_order}"}


@router.post("/admin/reset-contestant")
def reset_contestant_elimination(
    data: ResetContestantRequest,
    request: Request,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    require_admin(request)
    require_active_season(season)
    contestant = db.query(Contestant).filter(Contestant.id == data.contestant_id).first()
    if not contestant:
        raise HTTPException(status_code=404, detail="Contestant not found")

    contestant.elimination_order = None
    contestant.is_removed = False
    db.commit()

    # Unlock this season's rankings only if no game events remain
    active_count = (
        db.query(Contestant)
        .filter(
            Contestant.season_id == season.id,
            (Contestant.elimination_order.isnot(None)) | (Contestant.is_removed == True)
        )
        .count()
    )
    if active_count == 0:
        db.query(Ranking).filter(Ranking.season_id == season.id).update({"locked": False})
        db.commit()

    return {"message": f"{contestant.name} reset"}


@router.post("/admin/remove-contestant")
def remove_contestant(
    data: RemoveContestantRequest,
    request: Request,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    require_admin(request)
    require_active_season(season)
    if data.elimination_order < 1:
        raise HTTPException(status_code=400, detail="elimination_order (departure position) is required")

    contestant = db.query(Contestant).filter(Contestant.id == data.contestant_id).first()
    if not contestant:
        raise HTTPException(status_code=404, detail="Contestant not found")

    contestant.is_removed = True
    contestant.elimination_order = data.elimination_order
    db.commit()

    # Lock this season's rankings — a removal marks the start of game events
    db.query(Ranking).filter(Ranking.season_id == season.id).update({"locked": True})
    db.commit()

    return {"message": f"{contestant.name} marked as removed (departure #{data.elimination_order})"}


# --- Ranking endpoints ---

@router.get("/rankings")
def get_my_rankings(
    request: Request,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    user_id = get_current_user_id(request)

    rankings = (
        db.query(Ranking, Contestant)
        .join(Contestant, Ranking.contestant_id == Contestant.id)
        .filter(Ranking.user_id == user_id, Ranking.season_id == season.id)
        .order_by(Ranking.rank)
        .all()
    )

    if not rankings:
        return []
    total = len(rankings)
    return [
        {
            "contestant_id": r.Ranking.contestant_id,
            "contestant_name": r.Contestant.name,
            "tribe": r.Contestant.tribe,
            "image_url": r.Contestant.image_url,
            "rank": r.Ranking.rank,
            "locked": r.Ranking.locked,
            "scoring_eligible": r.Ranking.scoring_eligible,
            "elimination_order": r.Contestant.elimination_order,
            "is_winner": r.Contestant.elimination_order is not None and r.Contestant.elimination_order == total,
        }
        for r in rankings
    ]


@router.post("/rankings/email")
def email_my_rankings(
    request: Request,
    background_tasks: BackgroundTasks,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    user_id = get_current_user_id(request)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    rankings = (
        db.query(Ranking, Contestant)
        .join(Contestant, Ranking.contestant_id == Contestant.id)
        .filter(Ranking.user_id == user_id, Ranking.season_id == season.id)
        .order_by(Ranking.rank)
        .all()
    )
    if not rankings:
        raise HTTPException(status_code=400, detail="No rankings saved for this season")

    tribe_colors = {
        t.name: t.color
        for t in db.query(TribeConfig).filter(TribeConfig.season_id == season.id).all()
    }
    rankings_data = [
        {"rank": r.Ranking.rank, "contestant_name": r.Contestant.name, "tribe": r.Contestant.tribe}
        for r in rankings
    ]
    background_tasks.add_task(
        send_rankings_email,
        to_email=user.email,
        user_name=user.name,
        season_name=season.name,
        rankings=rankings_data,
        tribe_colors=tribe_colors,
    )
    return {"message": "Rankings email is being sent!"}


@router.post("/rankings")
def submit_rankings(
    submission: RankingSubmission,
    request: Request,
    background_tasks: BackgroundTasks,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    user_id = get_current_user_id(request)
    require_active_season(season)

    # Check if this season's rankings are locked
    existing = db.query(Ranking).filter(
        Ranking.user_id == user_id, Ranking.season_id == season.id, Ranking.locked == True
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Rankings are locked after eliminations begin")

    # Validate against this season's contestant count
    total_contestants = db.query(Contestant).filter(Contestant.season_id == season.id).count()
    if len(submission.rankings) != total_contestants:
        raise HTTPException(
            status_code=400,
            detail=f"Must rank all {total_contestants} contestants"
        )

    ranks = [r.rank for r in submission.rankings]
    if sorted(ranks) != list(range(1, total_contestants + 1)):
        raise HTTPException(status_code=400, detail="Ranks must be 1 through " + str(total_contestants))

    contestant_ids = [r.contestant_id for r in submission.rankings]
    if len(set(contestant_ids)) != total_contestants:
        raise HTTPException(status_code=400, detail="Each contestant must be ranked exactly once")

    # Clear existing rankings for this season
    db.query(Ranking).filter(Ranking.user_id == user_id, Ranking.season_id == season.id).delete()

    # Check which contestants have already departed in this season
    departed_ids = set(
        c.id for c in db.query(Contestant).filter(
            Contestant.season_id == season.id,
            Contestant.elimination_order.isnot(None)
        ).all()
    )
    season_started = len(departed_ids) > 0

    # Insert new rankings
    for r in submission.rankings:
        ranking = Ranking(
            user_id=user_id,
            season_id=season.id,
            contestant_id=r.contestant_id,
            rank=r.rank,
            locked=season_started,
            scoring_eligible=r.contestant_id not in departed_ids,
        )
        db.add(ranking)

    db.commit()

    # Audit log — append-only record of every ranking submission
    try:
        audit_sub = RankingAuditSubmission(
            user_id=user_id,
            season_id=season.id,
            session_user_email=request.session.get("user_email"),
            session_user_name=request.session.get("user_name"),
            client_ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
            contestant_count=len(submission.rankings),
        )
        db.add(audit_sub)
        db.flush()
        saved = (
            db.query(Ranking, Contestant)
            .join(Contestant, Ranking.contestant_id == Contestant.id)
            .filter(Ranking.user_id == user_id, Ranking.season_id == season.id)
            .all()
        )
        for r_rank, r_contestant in saved:
            db.add(RankingAuditEntry(
                submission_id=audit_sub.id,
                contestant_id=r_contestant.id,
                contestant_name=r_contestant.name,
                rank=r_rank.rank,
            ))
        db.commit()
    except Exception:
        logger.warning("Failed to write ranking audit log for user %s", user_id)

    # Queue rankings email in background (non-blocking)
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        tribe_colors = {
            t.name: t.color
            for t in db.query(TribeConfig).filter(TribeConfig.season_id == season.id).all()
        }
        rankings_data = [
            {"rank": r.rank, "contestant_name": r.contestant.name, "tribe": r.contestant.tribe}
            for r in db.query(Ranking).filter(
                Ranking.user_id == user_id, Ranking.season_id == season.id
            ).order_by(Ranking.rank).all()
        ]
        background_tasks.add_task(
            send_rankings_email,
            to_email=user.email,
            user_name=user.name,
            season_name=season.name,
            rankings=rankings_data,
            tribe_colors=tribe_colors,
        )

    if season_started and departed_ids:
        ineligible_count = len(departed_ids)
        return {
            "message": f"Rankings saved! {ineligible_count} contestant(s) already eliminated — no points for those picks.",
            "late_submission": True,
            "ineligible_count": ineligible_count,
        }

    return {"message": "Rankings saved successfully"}


# --- User management endpoints ---

@router.get("/admin/users")
def get_all_users(
    request: Request,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    require_admin(request)
    users = db.query(User).order_by(User.created_at).all()
    users_with_rankings = set(
        r[0] for r in db.query(Ranking.user_id).filter(Ranking.season_id == season.id).distinct().all()
    )
    return [
        {
            "id": u.id,
            "email": u.email,
            "name": u.name,
            "picture": u.picture,
            "is_admin": u.is_admin,
            "created_at": u.created_at.isoformat(),
            "has_rankings": u.id in users_with_rankings,
        }
        for u in users
    ]


@router.get("/admin/users/{user_id}/rankings")
def get_user_rankings_admin(
    user_id: int,
    request: Request,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    require_admin(request)
    rankings = (
        db.query(Ranking)
        .filter(Ranking.user_id == user_id, Ranking.season_id == season.id)
        .order_by(Ranking.rank)
        .all()
    )
    if not rankings:
        return []
    total = len(rankings)
    return [
        {
            "rank": r.rank,
            "contestant_name": r.contestant.name,
            "tribe": r.contestant.tribe,
            "elimination_order": r.contestant.elimination_order,
            "is_winner": r.contestant.elimination_order is not None and r.contestant.elimination_order == total,
            "is_removed": r.contestant.is_removed,
            "scoring_eligible": r.scoring_eligible,
        }
        for r in rankings
    ]


@router.delete("/admin/users/{user_id}/rankings")
def delete_user_rankings(
    user_id: int,
    request: Request,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    require_admin(request)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    db.query(Ranking).filter(Ranking.user_id == user_id, Ranking.season_id == season.id).delete()
    db.commit()
    return {"message": f"Rankings cleared for {user.name}"}


@router.get("/users/{user_id}/rankings")
def get_user_rankings_public(
    user_id: int,
    request: Request,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    get_current_user_id(request)
    rankings = (
        db.query(Ranking)
        .filter(Ranking.user_id == user_id, Ranking.season_id == season.id)
        .order_by(Ranking.rank)
        .all()
    )
    if not rankings:
        return []
    total = len(rankings)
    return [
        {
            "rank": r.rank,
            "contestant_name": r.contestant.name,
            "tribe": r.contestant.tribe,
            "elimination_order": r.contestant.elimination_order,
            "is_winner": r.contestant.elimination_order is not None and r.contestant.elimination_order == total,
            "is_removed": r.contestant.is_removed,
            "scoring_eligible": r.scoring_eligible,
        }
        for r in rankings
    ]


@router.post("/admin/users/{user_id}/role")
def update_user_role(
    user_id: int,
    update: RoleUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    current_admin_id = require_admin(request)

    if user_id == current_admin_id and not update.is_admin:
        raise HTTPException(status_code=400, detail="Cannot remove your own admin privileges")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_admin = update.is_admin
    db.commit()

    return {
        "message": f"{user.name} is now {'an admin' if update.is_admin else 'a standard user'}",
        "user_id": user.id,
        "is_admin": user.is_admin,
    }


# --- Score endpoints ---

@router.get("/scores")
def get_my_scores(
    request: Request,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    user_id = get_current_user_id(request)

    rankings = (
        db.query(Ranking, Contestant)
        .join(Contestant, Ranking.contestant_id == Contestant.id)
        .filter(Ranking.user_id == user_id, Ranking.season_id == season.id)
        .order_by(Ranking.rank)
        .all()
    )

    # Fetch graded bonus answers for this user+season
    bonus_rows = (
        db.query(BonusAnswer, BonusQuestion)
        .join(BonusQuestion, BonusAnswer.question_id == BonusQuestion.id)
        .filter(BonusAnswer.user_id == user_id, BonusQuestion.season_id == season.id,
                BonusAnswer.points_earned.isnot(None))
        .all()
    )
    bonus_total = sum(ba.points_earned for ba, bq in bonus_rows)
    bonus_breakdown = [
        {
            "question_id": bq.id,
            "question_text": bq.question_text,
            "answer_text": ba.answer_text,
            "wager": ba.wager,
            "outcome": ba.outcome,
            "points_earned": ba.points_earned,
        }
        for ba, bq in bonus_rows
    ]

    if not rankings:
        return {
            "total_score": bonus_total,
            "ranking_score": 0,
            "bonus_total": bonus_total,
            "bonus_questions": bonus_breakdown,
            "max_possible": 0,
            "contestants_scored": 0,
            "breakdown": [],
        }

    total_contestants = db.query(Contestant).filter(Contestant.season_id == season.id).count()
    ranking_data = [
        {
            "rank": r.Ranking.rank,
            "elimination_order": r.Contestant.elimination_order,
            "contestant_name": r.Contestant.name,
            "is_removed": r.Contestant.is_removed,
            "scoring_eligible": r.Ranking.scoring_eligible,
        }
        for r in rankings
    ]

    scores = calculate_total_score(ranking_data, total_contestants)
    return {
        **scores,
        "ranking_score": scores["total_score"],
        "total_score": scores["total_score"] + bonus_total,
        "bonus_total": bonus_total,
        "bonus_questions": bonus_breakdown,
    }


@router.get("/leaderboard")
def get_leaderboard(
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(User, Ranking, Contestant)
        .join(Ranking, Ranking.user_id == User.id)
        .join(Contestant, Ranking.contestant_id == Contestant.id)
        .filter(Ranking.season_id == season.id)
        .order_by(User.id, Ranking.rank)
        .all()
    )

    if not rows:
        return []

    # Group rankings by user in a single pass
    user_meta: dict[int, User] = {}
    user_rankings: dict[int, list] = {}
    for user, ranking, contestant in rows:
        if user.id not in user_meta:
            user_meta[user.id] = user
            user_rankings[user.id] = []
        user_rankings[user.id].append({
            "rank": ranking.rank,
            "elimination_order": contestant.elimination_order,
            "contestant_name": contestant.name,
            "is_removed": contestant.is_removed,
            "scoring_eligible": ranking.scoring_eligible,
        })

    # submit_rankings validates that all contestants must be ranked, so any user's
    # ranking count equals total_contestants — no separate count query needed.
    total_contestants = len(next(iter(user_rankings.values())))

    # Fetch bonus point totals per user for this season in one query
    bonus_rows = (
        db.query(BonusAnswer.user_id, func.sum(BonusAnswer.points_earned))
        .join(BonusQuestion, BonusAnswer.question_id == BonusQuestion.id)
        .filter(BonusQuestion.season_id == season.id, BonusAnswer.points_earned.isnot(None))
        .group_by(BonusAnswer.user_id)
        .all()
    )
    bonus_by_user: dict[int, int] = {uid: pts for uid, pts in bonus_rows}

    leaderboard = []
    for user_id, ranking_data in user_rankings.items():
        user = user_meta[user_id]
        scores = calculate_total_score(ranking_data, total_contestants)
        bonus_total = bonus_by_user.get(user_id, 0)
        leaderboard.append({
            "user_id": user.id,
            "user_name": user.name,
            "user_picture": user.picture,
            "ranking_score": scores["total_score"],
            "bonus_total": bonus_total,
            "total_score": scores["total_score"] + bonus_total,
            "max_possible": scores["max_possible"],
            "contestants_scored": scores["contestants_scored"],
        })

    leaderboard.sort(key=lambda x: x["total_score"], reverse=True)
    return leaderboard


# --- Bonus question endpoints ---

def _serialize_question(q: BonusQuestion, user_id: Optional[int], now_utc: datetime) -> dict:
    """Serialize a BonusQuestion with the current user's answer and, after deadline, all answers."""
    is_past_deadline = now_utc >= q.deadline_utc

    my_answer = None
    all_answers = []

    if user_id is not None:
        for ba in q.answers:
            if ba.user_id == user_id:
                my_answer = {
                    "id": ba.id,
                    "answer_text": ba.answer_text,
                    "wager": ba.wager,
                    "outcome": ba.outcome,
                    "points_earned": ba.points_earned,
                    "submitted_at": ba.submitted_at.isoformat(),
                }
                break

    if is_past_deadline:
        all_answers = [
            {
                "user_id": ba.user_id,
                "user_name": ba.user.name,
                "answer_text": ba.answer_text,
                "wager": ba.wager,
                "outcome": ba.outcome,
                "points_earned": ba.points_earned,
            }
            for ba in q.answers
        ]

    result = {
        "id": q.id,
        "season_id": q.season_id,
        "question_text": q.question_text,
        "question_type": q.question_type,
        "answer_type": q.answer_type or "string",
        "deadline_utc": q.deadline_utc.isoformat(),
        "is_past_deadline": is_past_deadline,
        "my_answer": my_answer,
        "all_answers": all_answers,
    }
    if q.question_type == "standard":
        result["points_value"] = q.points_value
        result["partial_points_value"] = q.partial_points_value
    else:
        result["max_wager"] = q.max_wager
    return result


@router.get("/bonus-questions")
def get_bonus_questions(
    request: Request,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    user_id = request.session.get("user_id")
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    questions = (
        db.query(BonusQuestion)
        .filter(BonusQuestion.season_id == season.id)
        .order_by(BonusQuestion.id)
        .all()
    )
    return [_serialize_question(q, user_id, now_utc) for q in questions]


@router.post("/bonus-questions/{question_id}/answer")
def submit_bonus_answer(
    question_id: int,
    data: BonusAnswerSubmit,
    request: Request,
    db: Session = Depends(get_db),
):
    user_id = get_current_user_id(request)
    question = db.query(BonusQuestion).filter(BonusQuestion.id == question_id).first()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    if now_utc >= question.deadline_utc:
        raise HTTPException(status_code=400, detail="Deadline has passed — answers can no longer be submitted or changed")

    answer_text = data.answer_text.strip()
    if not answer_text:
        raise HTTPException(status_code=400, detail="Answer cannot be empty")

    # Validate and normalise by answer_type
    answer_type = question.answer_type or "string"
    if answer_type == "contestant":
        match = db.query(Contestant).filter(
            Contestant.season_id == question.season_id,
            Contestant.name == answer_text,
        ).first()
        if not match:
            raise HTTPException(status_code=400, detail="Invalid contestant — please select from the list")
    elif answer_type == "integer":
        try:
            int(answer_text)
        except ValueError:
            raise HTTPException(status_code=400, detail="Answer must be a whole number")
    else:  # string — normalise to lowercase for case-insensitive comparison
        answer_text = answer_text.lower()

    if question.question_type == "wager":
        if data.wager is None:
            raise HTTPException(status_code=400, detail="Wager amount is required for wager questions")
        if data.wager < 1 or data.wager > question.max_wager:
            raise HTTPException(status_code=400, detail=f"Wager must be between 1 and {question.max_wager}")

    existing = db.query(BonusAnswer).filter(
        BonusAnswer.question_id == question_id,
        BonusAnswer.user_id == user_id,
    ).first()

    if existing:
        existing.answer_text = answer_text
        existing.wager = data.wager if question.question_type == "wager" else None
        existing.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    else:
        existing = BonusAnswer(
            question_id=question_id,
            user_id=user_id,
            answer_text=answer_text,
            wager=data.wager if question.question_type == "wager" else None,
        )
        db.add(existing)

    db.commit()
    db.refresh(existing)
    return {
        "id": existing.id,
        "answer_text": existing.answer_text,
        "wager": existing.wager,
        "outcome": existing.outcome,
        "points_earned": existing.points_earned,
        "submitted_at": existing.submitted_at.isoformat(),
        "message": "Answer saved",
    }


@router.post("/admin/bonus-questions")
def create_bonus_question(
    data: BonusQuestionCreate,
    request: Request,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    require_admin(request)
    require_active_season(season)

    if data.question_type not in ("standard", "wager"):
        raise HTTPException(status_code=400, detail="question_type must be 'standard' or 'wager'")
    if data.answer_type not in ("contestant", "integer", "string"):
        raise HTTPException(status_code=400, detail="answer_type must be 'contestant', 'integer', or 'string'")
    if data.question_type == "standard" and data.points_value is None:
        raise HTTPException(status_code=400, detail="points_value is required for standard questions")
    if data.question_type == "wager" and data.max_wager is None:
        raise HTTPException(status_code=400, detail="max_wager is required for wager questions")

    try:
        deadline = datetime.fromisoformat(data.deadline_utc.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid deadline format — use ISO 8601 UTC")

    q = BonusQuestion(
        season_id=season.id,
        question_text=data.question_text.strip(),
        question_type=data.question_type,
        answer_type=data.answer_type,
        deadline_utc=deadline,
        points_value=data.points_value,
        partial_points_value=data.partial_points_value or 0,
        max_wager=data.max_wager,
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    return {**_serialize_question(q, None, now_utc), "message": "Bonus question created"}


@router.patch("/admin/bonus-questions/{question_id}")
def update_bonus_question(
    question_id: int,
    data: BonusQuestionUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    q = db.query(BonusQuestion).filter(BonusQuestion.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Question not found")

    if data.question_text is not None:
        q.question_text = data.question_text.strip()
    if data.answer_type is not None:
        if data.answer_type not in ("contestant", "integer", "string"):
            raise HTTPException(status_code=400, detail="answer_type must be 'contestant', 'integer', or 'string'")
        q.answer_type = data.answer_type
    if data.deadline_utc is not None:
        try:
            q.deadline_utc = datetime.fromisoformat(data.deadline_utc.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid deadline format — use ISO 8601 UTC")
    if data.points_value is not None:
        q.points_value = data.points_value
    if data.partial_points_value is not None:
        q.partial_points_value = data.partial_points_value
    if data.max_wager is not None:
        q.max_wager = data.max_wager

    db.commit()
    db.refresh(q)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    return {**_serialize_question(q, None, now_utc), "message": "Bonus question updated"}


@router.delete("/admin/bonus-questions/{question_id}")
def delete_bonus_question(
    question_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    q = db.query(BonusQuestion).filter(BonusQuestion.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Question not found")
    db.delete(q)
    db.commit()
    return {"message": "Bonus question deleted"}


@router.post("/admin/bonus-questions/{question_id}/grade")
def grade_bonus_answer(
    question_id: int,
    data: BonusGrade,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)

    if data.outcome not in ("correct", "partial", "incorrect"):
        raise HTTPException(status_code=400, detail="outcome must be 'correct', 'partial', or 'incorrect'")

    q = db.query(BonusQuestion).filter(BonusQuestion.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Question not found")

    ba = db.query(BonusAnswer).filter(
        BonusAnswer.question_id == question_id,
        BonusAnswer.user_id == data.user_id,
    ).first()
    if not ba:
        raise HTTPException(status_code=404, detail="Answer not found for this user")

    if q.question_type == "standard":
        if data.outcome == "correct":
            ba.points_earned = q.points_value
        elif data.outcome == "partial":
            ba.points_earned = q.partial_points_value or 0
        else:
            ba.points_earned = 0
    else:  # wager
        if data.outcome == "correct":
            ba.points_earned = ba.wager
        elif data.outcome == "partial":
            ba.points_earned = 0
        else:
            ba.points_earned = -(ba.wager)

    ba.outcome = data.outcome
    db.commit()
    db.refresh(ba)
    return {
        "id": ba.id,
        "user_id": ba.user_id,
        "answer_text": ba.answer_text,
        "wager": ba.wager,
        "outcome": ba.outcome,
        "points_earned": ba.points_earned,
        "message": f"Answer graded as '{data.outcome}'",
    }


@router.post("/admin/send-email")
def send_broadcast(
    payload: BroadcastEmailRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    require_admin(request)
    if not payload.subject.strip() or not payload.body_text.strip():
        raise HTTPException(status_code=400, detail="Subject and body are required")
    if not payload.user_ids:
        raise HTTPException(status_code=400, detail="No recipients selected")
    users = db.query(User).filter(User.id.in_(payload.user_ids)).all()
    for user in users:
        background_tasks.add_task(
            send_broadcast_email,
            user.email, user.name,
            payload.subject.strip(), payload.body_html, payload.body_text.strip(),
        )
    return {"sent_to": len(users), "email_configured": is_email_configured()}


# --- Discussion helpers ---

VALID_REACTIONS = {"like", "heart", "sad"}
POSTS_PER_PAGE = 25


def format_display_name(full_name: str) -> str:
    """Convert 'John Doe' to 'John D.' format."""
    parts = full_name.strip().split()
    if len(parts) < 2:
        return full_name
    return f"{parts[0]} {parts[-1][0]}."


def _get_thread_or_404(thread_id: int, db: Session) -> EpisodeThread:
    thread = db.query(EpisodeThread).filter(EpisodeThread.id == thread_id).first()
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    return thread


# --- Discussion endpoints ---

@router.get("/discussions")
def get_discussions(
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    threads = (
        db.query(EpisodeThread)
        .filter(EpisodeThread.season_id == season.id)
        .order_by(EpisodeThread.episode_number)
        .all()
    )
    return [
        {
            "id": t.id,
            "episode_number": t.episode_number,
            "title": t.title,
            "post_count": len(t.posts),
            "created_at": t.created_at.isoformat(),
        }
        for t in threads
    ]


@router.post("/admin/discussions")
def create_episode_thread(
    data: EpisodeThreadCreate,
    request: Request,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    require_admin(request)
    require_active_season(season)

    if data.episode_number < 1:
        raise HTTPException(status_code=400, detail="Episode number must be at least 1")
    if season.episode_count and data.episode_number > season.episode_count:
        raise HTTPException(
            status_code=400,
            detail=f"Episode number exceeds season limit of {season.episode_count}",
        )

    existing = db.query(EpisodeThread).filter(
        EpisodeThread.season_id == season.id,
        EpisodeThread.episode_number == data.episode_number,
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Episode {data.episode_number} thread already exists")

    thread = EpisodeThread(
        season_id=season.id,
        episode_number=data.episode_number,
        title=data.title.strip(),
    )
    db.add(thread)
    db.commit()
    db.refresh(thread)
    return {
        "id": thread.id,
        "episode_number": thread.episode_number,
        "title": thread.title,
        "post_count": 0,
        "created_at": thread.created_at.isoformat(),
    }


@router.patch("/admin/discussions/{thread_id}")
def rename_episode_thread(
    thread_id: int,
    data: EpisodeThreadUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    thread = _get_thread_or_404(thread_id, db)
    require_active_season(thread.season)

    title = data.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be empty")

    thread.title = title
    db.commit()
    return {"id": thread.id, "title": thread.title, "message": "Thread renamed"}


@router.delete("/admin/discussions/{thread_id}")
def delete_episode_thread(
    thread_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    thread = _get_thread_or_404(thread_id, db)

    db.delete(thread)
    db.commit()
    return {"message": f"Episode {thread.episode_number} thread deleted"}


@router.get("/discussions/{thread_id}/posts")
def get_thread_posts(
    thread_id: int,
    request: Request,
    db: Session = Depends(get_db),
    page: int = 1,
):
    thread = _get_thread_or_404(thread_id, db)

    # Try to get current user for user_reactions (optional, no 401 if not logged in)
    current_user_id = request.session.get("user_id")

    total_posts = db.query(DiscussionPost).filter(DiscussionPost.thread_id == thread.id).count()
    total_pages = max(1, (total_posts + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE)

    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages

    offset = (page - 1) * POSTS_PER_PAGE
    posts = (
        db.query(DiscussionPost)
        .filter(DiscussionPost.thread_id == thread.id)
        .order_by(DiscussionPost.created_at.asc())
        .offset(offset)
        .limit(POSTS_PER_PAGE)
        .all()
    )

    result = []
    for p in posts:
        # Count reactions by type
        reaction_counts = {"like": 0, "heart": 0, "sad": 0}
        user_reactions = []
        for r in p.reactions:
            if r.reaction_type in reaction_counts:
                reaction_counts[r.reaction_type] += 1
            if current_user_id and r.user_id == current_user_id:
                user_reactions.append(r.reaction_type)

        result.append({
            "id": p.id,
            "user_id": p.user_id,
            "display_name": format_display_name(p.user.name),
            "user_picture": p.user.picture,
            "content": p.content,
            "is_edited": p.is_edited,
            "created_at": p.created_at.isoformat(),
            "reactions": reaction_counts,
            "user_reactions": user_reactions,
        })

    return {
        "posts": result,
        "total_posts": total_posts,
        "page": page,
        "total_pages": total_pages,
    }


@router.post("/discussions/{thread_id}/posts")
def create_post(
    thread_id: int,
    data: PostCreate,
    request: Request,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    user_id = get_current_user_id(request)
    require_active_season(season)
    thread = _get_thread_or_404(thread_id, db)

    if thread.season_id != season.id:
        raise HTTPException(status_code=400, detail="Thread does not belong to this season")

    content = data.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Post content cannot be empty")
    if len(content) > 500:
        raise HTTPException(status_code=400, detail="Post content cannot exceed 500 characters")

    post = DiscussionPost(
        thread_id=thread.id,
        user_id=user_id,
        content=content,
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    user = db.query(User).filter(User.id == user_id).first()
    return {
        "id": post.id,
        "user_id": post.user_id,
        "display_name": format_display_name(user.name),
        "user_picture": user.picture,
        "content": post.content,
        "is_edited": post.is_edited,
        "created_at": post.created_at.isoformat(),
        "reactions": {"like": 0, "heart": 0, "sad": 0},
        "user_reactions": [],
    }


@router.patch("/discussions/posts/{post_id}")
def edit_post(
    post_id: int,
    data: PostUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    user_id = get_current_user_id(request)

    post = db.query(DiscussionPost).filter(DiscussionPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.user_id != user_id:
        raise HTTPException(status_code=403, detail="You can only edit your own posts")

    # Check season is active
    thread = post.thread
    season = thread.season
    require_active_season(season)

    content = data.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Post content cannot be empty")
    if len(content) > 500:
        raise HTTPException(status_code=400, detail="Post content cannot exceed 500 characters")

    post.content = content
    post.is_edited = True
    db.commit()
    db.refresh(post)

    return {
        "id": post.id,
        "content": post.content,
        "is_edited": post.is_edited,
        "updated_at": post.updated_at.isoformat(),
    }


@router.delete("/admin/discussions/posts/{post_id}")
def delete_post(
    post_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)

    post = db.query(DiscussionPost).filter(DiscussionPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    db.delete(post)
    db.commit()
    return {"message": "Post deleted"}


@router.post("/discussions/posts/{post_id}/reactions")
def toggle_reaction(
    post_id: int,
    data: ReactionToggle,
    request: Request,
    db: Session = Depends(get_db),
):
    user_id = get_current_user_id(request)

    if data.reaction_type not in VALID_REACTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid reaction. Must be one of: {', '.join(sorted(VALID_REACTIONS))}")

    post = db.query(DiscussionPost).filter(DiscussionPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    existing = db.query(PostReaction).filter(
        PostReaction.post_id == post_id,
        PostReaction.user_id == user_id,
        PostReaction.reaction_type == data.reaction_type,
    ).first()

    if existing:
        # Toggle off — remove this reaction
        db.delete(existing)
    else:
        # Toggle on — add this reaction
        reaction = PostReaction(
            post_id=post_id,
            user_id=user_id,
            reaction_type=data.reaction_type,
        )
        db.add(reaction)

    db.commit()

    # Return updated counts and user's active reactions
    reaction_counts = {"like": 0, "heart": 0, "sad": 0}
    user_reactions = []
    for r in db.query(PostReaction).filter(PostReaction.post_id == post_id).all():
        if r.reaction_type in reaction_counts:
            reaction_counts[r.reaction_type] += 1
        if r.user_id == user_id:
            user_reactions.append(r.reaction_type)

    return {"reactions": reaction_counts, "user_reactions": user_reactions}


@router.patch("/admin/seasons/{season_id}/episode-count")
def update_episode_count(
    season_id: int,
    data: EpisodeCountUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)

    season = db.query(Season).filter(Season.id == season_id).first()
    if not season:
        raise HTTPException(status_code=404, detail="Season not found")

    if data.episode_count is not None and data.episode_count < 1:
        raise HTTPException(status_code=400, detail="Episode count must be at least 1")

    season.episode_count = data.episode_count
    db.commit()
    return {"message": f"Episode count updated to {data.episode_count}", "episode_count": season.episode_count}


# --- Database backup/restore endpoints ---

SQLITE_MAGIC = b"SQLite format 3\x00"


@router.get("/admin/database/export")
def export_database(request: Request):
    require_admin(request)
    db_path = get_db_path()
    if not os.path.exists(db_path):
        raise HTTPException(status_code=404, detail="Database file not found")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"survivor-backup-{timestamp}.db"
    return FileResponse(
        path=db_path,
        filename=filename,
        media_type="application/octet-stream",
    )


@router.post("/admin/database/import")
async def import_database(request: Request, file: UploadFile = File(...)):
    require_admin(request)

    contents = await file.read()

    if len(contents) < 16 or contents[:16] != SQLITE_MAGIC:
        raise HTTPException(status_code=400, detail="Invalid file — not a SQLite database")

    db_path = get_db_path()
    upload_path = db_path + ".upload"

    try:
        with open(upload_path, "wb") as f:
            f.write(contents)

        engine.dispose()
        shutil.move(upload_path, db_path)
    except Exception:
        if os.path.exists(upload_path):
            os.remove(upload_path)
        raise HTTPException(status_code=500, detail="Failed to restore database")

    return {"message": "Database restored successfully. Reload the page to see the updated data."}


# --- Ranking audit log endpoints ---

@router.get("/admin/audit/rankings")
def get_audit_submissions(
    request: Request,
    user_id: int,
    season: Season = Depends(get_season),
    db: Session = Depends(get_db),
):
    require_admin(request)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    submissions = (
        db.query(RankingAuditSubmission)
        .filter(
            RankingAuditSubmission.user_id == user_id,
            RankingAuditSubmission.season_id == season.id,
        )
        .order_by(RankingAuditSubmission.created_at.desc())
        .all()
    )
    return [
        {
            "id": s.id,
            "user_id": s.user_id,
            "user_name": user.name,
            "user_email": user.email,
            "session_user_email": s.session_user_email,
            "session_user_name": s.session_user_name,
            "client_ip": s.client_ip,
            "user_agent": s.user_agent,
            "contestant_count": s.contestant_count,
            "created_at": s.created_at.isoformat(),
        }
        for s in submissions
    ]


@router.get("/admin/audit/rankings/{submission_id}")
def get_audit_snapshot(
    submission_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    sub = db.query(RankingAuditSubmission).filter(RankingAuditSubmission.id == submission_id).first()
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    user = db.query(User).filter(User.id == sub.user_id).first()
    entries = (
        db.query(RankingAuditEntry)
        .filter(RankingAuditEntry.submission_id == sub.id)
        .order_by(RankingAuditEntry.rank)
        .all()
    )
    return {
        "id": sub.id,
        "user_id": sub.user_id,
        "user_name": user.name if user else "Unknown",
        "user_email": user.email if user else "",
        "session_user_email": sub.session_user_email,
        "session_user_name": sub.session_user_name,
        "client_ip": sub.client_ip,
        "user_agent": sub.user_agent,
        "contestant_count": sub.contestant_count,
        "created_at": sub.created_at.isoformat(),
        "entries": [
            {
                "rank": e.rank,
                "contestant_id": e.contestant_id,
                "contestant_name": e.contestant_name,
            }
            for e in entries
        ],
    }
