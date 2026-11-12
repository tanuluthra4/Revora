"""Application configuration, read from environment variables.

Nothing secret is hardcoded. See README.md for the full variable list.
"""
import os
from datetime import timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_DIR = os.path.join(BASE_DIR, "database")

# Kept for backwards compatibility with code that imports DB_PATH directly.
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(DATABASE_DIR, "revora.db"))



class Config:
    # "production" turns on Secure cookies and refuses to start without SECRET_KEY.
    ENV = os.getenv("REVORA_ENV", "development").lower()

    DATABASE = DB_PATH
    SECRET_KEY = os.getenv("SECRET_KEY") or None

    # Roles are exclusive account types chosen at sign-up: "developer" or "reviewer".
    # Anyone may register as either. An account alone proves nothing about identity,
    # expertise or affiliation; reviewer profiles are self-reported (see README).

    # Default approval policy for new submissions: this many reviewers must approve.
    DEFAULT_REQUIRED_APPROVALS = int(os.getenv("DEFAULT_REQUIRED_APPROVALS", "2"))

    # Optional. When set (e.g. https://your-app.onrender.com) the landing page
    # emits canonical and og:url tags. Left empty, no URL is invented.
    PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = ENV == "production"
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    MAX_CONTENT_LENGTH = 1024 * 1024  # 1 MB; there are no file uploads
    JSON_SORT_KEYS = True

    TESTING = False
