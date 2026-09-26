"""
Web UI + HTTP API for the PM Agent — the browser twin of `main.py`.

    uv run web.py                     # http://127.0.0.1:8000
    uv run web.py --port 8080

Same agent, same lanes, same approval gate as the CLI; the page adds a live
"thinking chain". The server runs in-process (one worker, no reload) and binds to
loopback only: it is a local tool with your Jira credentials behind it.
API reference: http://127.0.0.1:8000/api/docs
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from pmagent import env
from pmagent.web.app import create_app

LOOPBACK = {"127.0.0.1", "localhost"}
WILDCARD = {"0.0.0.0", "::"}


def build_server(host: str = "127.0.0.1", port: int = 8000,
                 allowed_hosts: tuple[str, ...] = ()) -> uvicorn.Server:
    # The Host allowlist holds names clients *send* — never a wildcard bind
    # address like 0.0.0.0, which no browser puts in a Host header.
    extra = tuple(allowed_hosts) + (() if host in LOOPBACK | WILDCARD else (host,))
    app = create_app(api_token=env.PMAGENT_API_TOKEN, extra_hosts=extra, enable_mcp=True)
    # In-process, single worker, no reload: a reloader or worker subprocess would
    # not inherit in-process setup such as scripts/smoke.py's write block.
    config = uvicorn.Config(app, host=host, port=port, workers=1, reload=False, log_level="warning")
    return uvicorn.Server(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PM Agent web UI")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interface to bind. Keep loopback unless you set PMAGENT_API_TOKEN.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--allowed-host", action="append", default=[], metavar="NAME",
                        help="Extra Host name clients will use (repeatable), e.g. the machine's "
                             "DNS name. Required when binding 0.0.0.0.")
    args = parser.parse_args(argv)

    try:
        env.validate()
        env.validate_jira()
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 1
    if args.host not in LOOPBACK:
        if not env.PMAGENT_API_TOKEN:
            print("Refusing to bind a non-loopback host without PMAGENT_API_TOKEN set.")
            return 1
        if args.host in WILDCARD and not args.allowed_host:
            print("Binding 0.0.0.0 needs --allowed-host <name> for each name clients will use.")
            return 1
        print("WARNING: this server speaks plain HTTP. Off this machine, put TLS in front of it "
              "(a reverse proxy) — otherwise the API token and your Jira data cross the network "
              "unencrypted.")

    from main import _quiet_library_logs

    _quiet_library_logs()
    # 0.0.0.0 is a bind address, not a URL a browser can use (the Host check refuses it).
    shown = "localhost" if args.host in WILDCARD else args.host
    print(f"PM Agent web UI → http://{shown}:{args.port}   (API docs: /api/docs)")
    print(f"Model: {env.LLM_PROVIDER} / {env.LLM_MODEL}   Jira: {env.JIRA_PROJECT_KEY}")
    build_server(args.host, args.port, tuple(args.allowed_host)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
