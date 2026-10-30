"""Production entrypoint for original posts: human-posted Telegram drafts by
default, official-API publishing only with the owner's explicit authorization."""
import asyncio
import sys

from .daemon import Daemon, daemon_lock

# sysexits EX_CONFIG: the unit file tells systemd not to restart on it.
NOT_AUTHORIZED_EXIT = 78


class OriginalPostsNotAuthorized(SystemExit):
    pass


def build_daemon() -> Daemon:
    """Two modes. With the owner's explicit authorization the service
    publishes originals through the X API. Otherwise it drafts them and sends
    each to Telegram; a person publishes it from the X app (no X API write)."""
    daemon = Daemon()
    publishing = daemon.cfg.publishing
    has_write_tokens = bool(
        getattr(daemon.cfg, "x_access_token", "")
        and getattr(daemon.cfg, "x_access_token_secret", "")
    )
    authorized = (
        bool(publishing.original_posts_authorized)
        and has_write_tokens
        # Review first: drafts to Telegram even with the API authorized.
        and not getattr(publishing, "original_posts_review_first", False)
    )
    if publishing.original_posts_authorized and not has_write_tokens:
        # Authorized, but the account's write keys are not installed yet: keep
        # delivering human-posted drafts instead of failing or retrying.
        print(
            "original posts authorized but X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET "
            "are missing; sending Telegram drafts until they are installed",
            file=sys.stderr,
        )
    drafts = not authorized and bool(getattr(publishing, "original_post_drafts", False))
    if not authorized and not drafts:
        raise OriginalPostsNotAuthorized(
            "original posts are not authorized: the account owner must set "
            "publishing.original_posts_authorized to true in config.json "
            "(or publishing.original_post_drafts for human-posted drafts)"
        )
    daemon.cfg.dry_run = False
    daemon.cfg.detection.enabled = False
    daemon.cfg.x_detection.enabled = False
    daemon.cfg.publishing.enabled = authorized
    daemon.cfg.human_approval.enabled = False
    daemon.post_drafts = drafts
    return daemon


def main() -> int:
    try:
        daemon = build_daemon()
    except OriginalPostsNotAuthorized as exc:
        print(exc, file=sys.stderr)
        return NOT_AUTHORIZED_EXIT
    with daemon_lock("/tmp/pentra-x-posts.lock"):
        asyncio.run(daemon.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
