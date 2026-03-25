from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import DATABASE_URL

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_path() -> str:
    """Extract the filesystem path from the SQLite DATABASE_URL."""
    if DATABASE_URL.startswith("sqlite:////"):
        return DATABASE_URL[len("sqlite:///"):]
    elif DATABASE_URL.startswith("sqlite:///"):
        return DATABASE_URL[len("sqlite:///"):]
    return DATABASE_URL
