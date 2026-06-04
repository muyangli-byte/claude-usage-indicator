"""CLI behavior tests that don't touch the network or GTK."""
from cui import cli


def test_self_update_refuses_on_dev(monkeypatch):
    """On a dev instance, --self-update must bail out BEFORE any git/reset, so it can't
    wipe the developer's working tree to origin/main."""
    monkeypatch.setattr(cli, "IS_DEV", True)
    written = {}
    monkeypatch.setattr(cli, "_write_update_result", lambda t: written.__setitem__("t", t))

    def boom(*a, **k):  # reaching subprocess means the guard failed
        raise AssertionError("cmd_self_update touched subprocess on a dev instance")
    monkeypatch.setattr(cli.subprocess, "run", boom)

    rc = cli.cmd_self_update()
    assert rc == 1
    assert "dev" in written.get("t", "").lower()
