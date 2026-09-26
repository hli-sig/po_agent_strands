"""web.py's binding rules (audit #5): loopback by default; anything else needs a
token, and a wildcard bind needs the real Host names clients will send."""

from starlette.testclient import TestClient

import web
from pmagent import env


def test_loopback_default_answers_loopback_names_only():
    app = web.build_server().config.app
    assert TestClient(app, base_url="http://127.0.0.1").get("/api/v1/health").status_code == 200
    assert TestClient(app, base_url="http://localhost").get("/api/v1/health").status_code == 200
    assert TestClient(app, base_url="http://pm.local").get("/api/v1/health").status_code == 400


def test_a_wildcard_bind_uses_the_allowed_host_names_not_the_bind_address():
    app = web.build_server("0.0.0.0", 9999, ("pm.local",)).config.app
    assert TestClient(app, base_url="http://pm.local").get("/api/v1/health").status_code == 200
    assert TestClient(app, base_url="http://0.0.0.0").get("/api/v1/health").status_code == 400


def test_main_refuses_unsafe_binds(monkeypatch, capsys):
    monkeypatch.setattr(env, "validate", lambda: None)
    monkeypatch.setattr(env, "validate_jira", lambda: None)
    monkeypatch.setattr(env, "PMAGENT_API_TOKEN", None)
    assert web.main(["--host", "192.168.1.10"]) == 1
    assert "PMAGENT_API_TOKEN" in capsys.readouterr().out
    monkeypatch.setattr(env, "PMAGENT_API_TOKEN", "t")
    assert web.main(["--host", "0.0.0.0"]) == 1
    assert "--allowed-host" in capsys.readouterr().out
