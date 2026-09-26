"""The Docker image: what goes into it, and what its entrypoint runs.

No Docker needed. These tests check the build context, the Dockerfile's
promises, and `scripts/container.py`, and one of them starts the entrypoint's
`demo` command for real, exactly as the container would.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from scripts import container

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _glob_regex(glob: str) -> re.Pattern:
    """Docker's .dockerignore globs: `*` and `?` stay within one path segment, `**` spans any."""
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "?":
            out, i = out + "[^/]", i + 1
        elif glob[i] == "[":
            close = glob.index("]", i)
            out, i = out + glob[i:close + 1], close + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return re.compile(out)


RULES = [(line.startswith("!"), _glob_regex(line.lstrip("!").rstrip("/")))
         for line in (ln.strip() for ln in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
         if line and not line.startswith("#")]


def in_build_context(path: str) -> bool:
    """Docker's rule: the last pattern matching the path, or a folder above it, wins."""
    parts = path.split("/")
    candidates = ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]
    included = True
    for negated, regex in RULES:
        if any(regex.fullmatch(c) for c in candidates):
            included = negated
    return included


@pytest.mark.parametrize("path", [
    "main.py", "web.py", "pyproject.toml", "uv.lock", "pmagent/gate.py",
    "pmagent/web/static/app.js", "scripts/container.py", "scripts/demo_web.py",
    "tests/fakes.py", "scripts/smoke.py", "docs/learning/examples/sprint_review/server.py",
    "docs/learning/examples/sprint_review/sample-jira.csv",
])
def test_the_app_is_in_the_image(path):
    assert (ROOT / path).exists()
    assert in_build_context(path)


@pytest.mark.parametrize("path", [
    ".env", ".env.local", ".git/config", ".venv/bin/python",
    "sprint-review-handover-v1.0.0.zip", "data/fy_budget/output/fy27.csv",
    "docs/evidence/smoke_transcript.txt", "docs/PLAN.md", "tests/test_gate.py",
    "pmagent/tools/fy_budget/fixtures/budget.xlsx", "pmagent/__pycache__/env.cpython-313.pyc",
    "anything-new-dropped-here.pptx", "pmagent/tools/fy_budget/PROVENANCE.md",
    "docs/learning/course.html", "docs/learning/08-mcp.md", "scripts/build_course.py",
])
def test_secrets_real_data_and_strays_are_not(path):
    assert not in_build_context(path)


def test_the_dockerfile_keeps_its_promises():
    assert "uv sync --locked --no-dev" in DOCKERFILE           # the lock file, no test tools
    assert "\nUSER app\n" in DOCKERFILE                        # not root
    assert 'ENTRYPOINT ["python", "scripts/container.py"]' in DOCKERFILE
    assert 'CMD ["demo"]' in DOCKERFILE                        # the safe default
    assert not [ln for ln in DOCKERFILE.splitlines() if ln.startswith("VOLUME")]  # no anonymous volumes
    assert "HOME=/app/data/home" in DOCKERFILE                 # the token cache is writable
    assert not [ln for ln in DOCKERFILE.splitlines() if ln.startswith(("COPY", "ADD")) and ".env" in ln]
    version = (ROOT / ".python-version").read_text().strip()
    assert f"ARG PYTHON_VERSION={version}" in DOCKERFILE


def test_the_examples_command_runs_every_example_but_the_one_needing_extras():
    examples = {p.name for p in (ROOT / "docs/learning/examples").glob("[0-9]*.py")}
    assert set(container.OFFLINE_EXAMPLES) == examples - container.NEEDS_EXTRAS
    assert "08_sprint_review_mcp.py" in container.OFFLINE_EXAMPLES


def test_a_mistyped_command_lists_the_commands(capsys):
    with pytest.raises(SystemExit) as exit_:
        container.main(["dmeo"])
    assert "Unknown command 'dmeo'" in str(exit_.value) and "sprint-review-mcp" in str(exit_.value)


def test_the_sprint_review_command_creates_its_root(tmp_path, monkeypatch):
    root = tmp_path / "data" / "sprint-review"
    monkeypatch.setattr(container, "SPRINT_REVIEW_ROOT", str(root))
    monkeypatch.setattr(container.os, "execvp", lambda *a: None)
    monkeypatch.chdir(tmp_path)
    container.main(["sprint-review-mcp"])
    assert root.is_dir()


def test_commands(monkeypatch):
    monkeypatch.setattr(container, "PORT", "8000")
    py = sys.executable
    assert container.command([]) == (
        [py, "scripts/demo_web.py", "--host", "0.0.0.0", "--port", "8000"], container.APP)
    assert container.command(["cli"])[0] == [py, "main.py"]
    assert container.command(["smoke"])[0] == [py, "scripts/smoke.py"]
    assert container.command(["bash", "-l"]) == (["bash", "-l"], container.APP)
    args, cwd = container.command(["sprint-review-mcp"])
    assert args[1:4] == ["-m", "sprint_review.server", "--root"] and cwd == container.EXAMPLES


def test_the_web_commands_allow_localhost_plus_configured_hosts(monkeypatch):
    monkeypatch.setenv("PMAGENT_ALLOWED_HOSTS", "pm.example.com, ,agent.local")
    args, _ = container.command(["web", "--port", "9000"])
    assert args[1:4] == ["web.py", "--host", "0.0.0.0"]
    hosts = [args[i + 1] for i, a in enumerate(args) if a == "--allowed-host"]
    assert hosts == ["localhost", "pm.example.com", "agent.local"]
    assert args[-2:] == ["--port", "9000"]
    smoke, _ = container.command(["smoke-web"])
    assert smoke[1:3] == ["scripts/smoke.py", "--web"] and "--allowed-host" in smoke


def test_web_in_a_container_still_refuses_to_start_without_a_token(monkeypatch):
    import web

    monkeypatch.setattr(web.env, "PMAGENT_API_TOKEN", None)
    monkeypatch.setattr(web.env, "validate", lambda: None)
    monkeypatch.setattr(web.env, "validate_jira", lambda: None)
    args, _ = container.command(["web"])
    assert web.main(args[2:]) == 1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_default_command_serves_the_demo_on_all_interfaces():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "scripts/container.py"], cwd=ROOT,
                            env={**os.environ, "PORT": str(port)},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 30
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=2) as r:
                    assert r.status == 200
                    break
            except OSError:
                assert proc.poll() is None, proc.stdout.read()
                assert time.time() < deadline, "demo did not start"
                time.sleep(0.2)
        bound = subprocess.run(["ss", "-ltn", f"sport = :{port}"], capture_output=True, text=True)
        if bound.returncode == 0:
            assert f"0.0.0.0:{port}" in bound.stdout
    finally:
        proc.terminate()
        proc.wait(timeout=10)
