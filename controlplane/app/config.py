import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str
    jwt_ttl_minutes: int
    reset_token_ttl_minutes: int
    invite_token_ttl_hours: int
    public_web_url: str
    cors_origins: tuple[str, ...]
    signup_enabled: bool


@lru_cache
def get_settings() -> Settings:
    secret = os.environ.get("JWT_SECRET", "")
    if len(secret) < 32:
        # No default on purpose: a guessable signing key means anyone can mint tokens.
        raise RuntimeError("JWT_SECRET must be set to at least 32 characters")
    return Settings(
        database_url=os.environ["CONTROL_DB_API_URL"],
        jwt_secret=secret,
        jwt_ttl_minutes=int(os.environ.get("JWT_TTL_MINUTES", "480")),
        reset_token_ttl_minutes=int(os.environ.get("RESET_TOKEN_TTL_MINUTES", "30")),
        invite_token_ttl_hours=int(os.environ.get("INVITE_TOKEN_TTL_HOURS", "72")),
        public_web_url=os.environ.get("PUBLIC_WEB_URL", "http://localhost:5173").rstrip("/"),
        cors_origins=tuple(
            o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()
        ),
        # Anyone who can reach the console can create an organization unless this is switched off.
        signup_enabled=os.environ.get("SIGNUP_ENABLED", "true").lower() not in ("0", "false", "no"),
    )
