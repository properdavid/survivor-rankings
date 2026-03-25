"""Authentication routes for Google OAuth."""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth
from sqlalchemy.orm import Session

from app.config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, BASE_URL, ADMIN_EMAIL
from app.database import get_db
from app.models import User


router = APIRouter(prefix="/auth", tags=["auth"])

oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


@router.get("/login")
async def login(request: Request):
    redirect_uri = f"{BASE_URL}/auth/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    token = await oauth.google.authorize_access_token(request)
    user_info = token.get("userinfo")

    if not user_info:
        return RedirectResponse(url="/?error=auth_failed")

    email = user_info["email"]
    name = user_info.get("name", email)
    picture = user_info.get("picture", "")

    # Find or create user
    db_user = db.query(User).filter(User.email == email).first()
    if db_user:
        db_user.name = name
        db_user.picture = picture
        if email == ADMIN_EMAIL:
            db_user.is_admin = True
    else:
        db_user = User(
            email=email,
            name=name,
            picture=picture,
            is_admin=(email == ADMIN_EMAIL),
        )
        db.add(db_user)

    db.commit()
    db.refresh(db_user)

    # Store user info in session
    request.session["user_id"] = db_user.id
    request.session["user_email"] = db_user.email
    request.session["user_name"] = db_user.name
    request.session["user_picture"] = db_user.picture or ""
    request.session["is_admin"] = db_user.is_admin

    return RedirectResponse(url="/")


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/")


@router.get("/me")
async def get_current_user(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"authenticated": False}

    db_user = db.query(User).filter(User.id == user_id).first()
    if not db_user:
        return {"authenticated": False}

    # Sync session if admin status was changed by another admin
    if db_user.is_admin != request.session.get("is_admin", False):
        request.session["is_admin"] = db_user.is_admin

    return {
        "authenticated": True,
        "user_id": user_id,
        "email": request.session.get("user_email"),
        "name": request.session.get("user_name"),
        "picture": request.session.get("user_picture"),
        "is_admin": db_user.is_admin,
    }
