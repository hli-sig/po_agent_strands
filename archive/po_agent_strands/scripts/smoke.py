"""
Live smoke-test launcher: the real CLI, the real model, the real Jira — with
every write path blocked.

    uv run scripts/smoke.py            # interactive CLI
    uv run scripts/smoke.py --web      # the web UI, same write block
    uv run scripts/smoke.py < turns.txt   # scripted (one user line per turn,
                                          # approval answers inline)

Why this exists: `.env` points at a production Jira tenant. Testing the approval
gate by hoping nobody types "y" — and hoping the gate under test has no bugs —
is not a safety mechanism. This launcher makes writes impossible underneath the
app, so the smoke test can exercise the *approve* path too: an approved write
must fail with "smoke: write blocked".

How: every HTTP call the domain code makes goes through `requests`, and every
`requests.get/post/...` call — and the Jira and Confluence `Session`s — ends
in `requests.Session.request`. That one method is wrapped with a fail-closed
allowlist:

    GET                                   allowed (reads)
    POST  .../rest/api/3/search/jql       allowed (Jira's JQL read path is a POST)
    POST  login.microsoftonline.com/.../oauth2/v2.0/token   allowed (token refresh)
    anything else                         RuntimeError("smoke: write blocked")

The LLM SDKs use httpx, not requests, so model calls are unaffected. Lucid MCP
also uses httpx, so it is force-disabled. The FY budget converter writes local
files, so it is replaced too. The Microsoft device-code login is a POST and is
blocked, so the spreadsheet lane can't be smoke-tested past sign-in (its tools
are all writes anyway).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

# Must happen before pmagent.env is imported anywhere.
os.environ["LUCID_MCP_ENABLED"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

BLOCKED = "smoke: write blocked"
_real_request = requests.Session.request


def allowed(method: str, url: str) -> bool:
    method = method.upper()
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    if method != "POST":
        return False
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")  # exact path — a query string can't whitelist anything
    if path.endswith("/rest/api/3/search/jql"):
        return True
    if parsed.hostname == "login.microsoftonline.com" and path.endswith("/oauth2/v2.0/token"):
        return True
    return False


def guarded_request(self, method, url, *args, **kwargs):
    if not allowed(method, url):
        print(f"\033[31m[smoke] BLOCKED {method.upper()} {urlparse(url).path}\033[0m")
        raise RuntimeError(BLOCKED)
    return _real_request(self, method, url, *args, **kwargs)


def blocked_conversion(*args, **kwargs):
    raise RuntimeError(BLOCKED)


def install() -> None:
    requests.Session.request = guarded_request

    import pmagent.tools.fy_budget.pipeline as pipeline

    # finance_tools imports convert_fy_budget inside the tool, at call time,
    # so replacing the module attribute is enough.
    pipeline.convert_fy_budget = blocked_conversion


def self_check() -> None:
    """Refuse to start unless the block demonstrably works."""
    for method, url in [
        ("POST", "https://x.atlassian.net/rest/api/3/issue"),
        ("POST", "https://x.atlassian.net/rest/api/3/issue/bulk"),
        ("PUT", "https://x.atlassian.net/rest/api/3/issue/CSCI-1"),
        ("POST", "https://x.atlassian.net/rest/api/3/issue/CSCI-1/comment"),
        ("POST", "https://x.atlassian.net/rest/api/3/issue?x=/rest/api/3/search/jql"),
        ("POST", "https://graph.microsoft.com/v1.0/me/drive/items/x/workbook/tables/t/rows/add"),
        ("POST", "https://login.microsoftonline.com/t/oauth2/v2.0/devicecode"),
    ]:
        try:
            requests.request(method, url, timeout=1)
        except RuntimeError as exc:
            assert str(exc) == BLOCKED
        else:
            raise SystemExit(f"smoke self-check FAILED: {method} {url} was not blocked")
    assert allowed("POST", "https://x.atlassian.net/rest/api/3/search/jql")
    assert allowed("GET", "https://x.atlassian.net/rest/api/3/issue/CSCI-1")

    import pmagent.tools.fy_budget.pipeline as pipeline

    try:
        pipeline.convert_fy_budget([], 202607, Path("x.csv"))
    except RuntimeError as exc:
        assert str(exc) == BLOCKED
    else:
        raise SystemExit("smoke self-check FAILED: FY conversion not blocked")

    from pmagent import env

    assert env.LUCID_MCP_ENABLED is False
    print("\033[2m[smoke] write block installed and self-checked\033[0m")


if __name__ == "__main__":
    install()
    self_check()
    if "--web" in sys.argv:
        # The web server runs in THIS process (web.build_server: one worker, no
        # reload), so the requests patch above covers every request it serves.
        import web

        assert requests.Session.request is guarded_request, "write block not installed"
        argv = [a for a in sys.argv[1:] if a != "--web"]
        sys.exit(web.main(argv))
    import main

    sys.exit(main.main())
