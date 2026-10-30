from types import SimpleNamespace

import pytest

from bot import posts_daemon, replies_daemon


class FakeDaemon:
    authorized = True

    def __init__(self):
        self.cfg = SimpleNamespace(
            dry_run=True,
            detection=SimpleNamespace(enabled=False),
            x_detection=SimpleNamespace(enabled=False),
            publishing=SimpleNamespace(
                enabled=False,
                original_posts_authorized=self.authorized,
            ),
            human_approval=SimpleNamespace(enabled=False),
            x_access_token="123-token",
            x_access_token_secret="token-secret",
        )


class UnauthorizedDaemon(FakeDaemon):
    authorized = False


def test_reply_role_owns_detection_but_never_public_publishing(monkeypatch):
    monkeypatch.setattr(replies_daemon, "Daemon", FakeDaemon)
    daemon = replies_daemon.build_daemon()
    assert not daemon.cfg.dry_run
    assert not daemon.cfg.detection.enabled
    assert daemon.cfg.x_detection.enabled
    assert not daemon.cfg.publishing.enabled
    assert daemon.cfg.human_approval.enabled


def test_post_role_owns_public_posts_but_never_paid_detection(monkeypatch):
    monkeypatch.setattr(posts_daemon, "Daemon", FakeDaemon)
    daemon = posts_daemon.build_daemon()
    assert not daemon.cfg.dry_run
    assert not daemon.cfg.detection.enabled
    assert not daemon.cfg.x_detection.enabled
    assert daemon.cfg.publishing.enabled
    assert not daemon.cfg.human_approval.enabled


def test_post_role_refuses_without_owner_authorization(monkeypatch):
    monkeypatch.setattr(posts_daemon, "Daemon", UnauthorizedDaemon)
    with pytest.raises(SystemExit, match="not authorized"):
        posts_daemon.build_daemon()


def test_original_posts_are_unauthorized_by_default():
    from bot.config import PublishingBudget

    assert PublishingBudget().original_posts_authorized is False


def test_unauthorized_posts_entrypoint_exits_with_config_code(monkeypatch, capsys):
    monkeypatch.setattr(posts_daemon, "Daemon", UnauthorizedDaemon)
    assert posts_daemon.main() == posts_daemon.NOT_AUTHORIZED_EXIT == 78
    assert "not authorized" in capsys.readouterr().err
    unit = open("deploy/pentra-x-posts.service").read()
    assert "RestartPreventExitStatus=78" in unit


class DraftsDaemon(FakeDaemon):
    authorized = False

    def __init__(self):
        super().__init__()
        self.cfg.publishing.original_post_drafts = True


def test_post_role_defaults_to_human_posted_drafts_without_authorization(monkeypatch):
    monkeypatch.setattr(posts_daemon, "Daemon", DraftsDaemon)
    daemon = posts_daemon.build_daemon()
    assert daemon.post_drafts is True
    assert not daemon.cfg.dry_run
    assert not daemon.cfg.detection.enabled
    assert not daemon.cfg.x_detection.enabled
    assert not daemon.cfg.publishing.enabled, "drafts never write to X"
    assert not daemon.cfg.human_approval.enabled


def test_post_drafts_are_the_config_default():
    from bot.config import PublishingBudget

    assert PublishingBudget().original_post_drafts is True


class AuthorizedNoTokensDaemon(FakeDaemon):
    authorized = True

    def __init__(self):
        super().__init__()
        self.cfg.publishing.original_post_drafts = True
        self.cfg.x_access_token = ""
        self.cfg.x_access_token_secret = ""


class AuthorizedWithTokensDaemon(AuthorizedNoTokensDaemon):
    def __init__(self):
        super().__init__()
        self.cfg.x_access_token = "123-abc"
        self.cfg.x_access_token_secret = "secret"


def test_authorized_posts_wait_for_write_tokens_and_keep_sending_drafts(monkeypatch, capsys):
    monkeypatch.setattr(posts_daemon, "Daemon", AuthorizedNoTokensDaemon)
    daemon = posts_daemon.build_daemon()
    assert daemon.post_drafts is True
    assert not daemon.cfg.publishing.enabled
    assert "X_ACCESS_TOKEN" in capsys.readouterr().err


def test_authorized_posts_with_write_tokens_publish_automatically(monkeypatch):
    monkeypatch.setattr(posts_daemon, "Daemon", AuthorizedWithTokensDaemon)
    daemon = posts_daemon.build_daemon()
    assert daemon.post_drafts is False
    assert daemon.cfg.publishing.enabled
    assert not daemon.cfg.human_approval.enabled
