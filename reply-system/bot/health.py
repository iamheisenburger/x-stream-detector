"""Official X publishing preflight."""

from .config import Config
from .poster import verify_account


async def check_session_alive(cfg: Config) -> bool:
    """Compatibility name retained for the daemon's startup preflight."""
    return await verify_account(cfg)
