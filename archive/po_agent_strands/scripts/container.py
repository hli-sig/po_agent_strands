"""
The Docker image's entrypoint: one named command per way to run the project.

    docker run --rm --init -p 127.0.0.1:8000:8000 po-agent            # demo (default)
    docker run --rm --init -p 127.0.0.1:8000:8000 --env-file .env po-agent web
    docker run --rm --init -it --env-file .env po-agent cli
    docker run --rm --init -it --env-file .env po-agent smoke         # CLI, writes blocked
    docker run --rm --init -p 127.0.0.1:8000:8000 --env-file .env po-agent smoke-web
    docker run --rm --init -i --network none -v po-agent-data:/app/data po-agent sprint-review-mcp
    docker run --rm --init po-agent examples                          # every lesson example, offline

Anything else is run as given (`docker run --rm -it po-agent bash`). Extra
arguments go to the command (`... web --allowed-host pm.example.com`).

Why the web commands bind 0.0.0.0: inside a container, 127.0.0.1 is the
container's own loopback, which the host can't reach. `web.py` still enforces
its rule for a non-loopback bind: `PMAGENT_API_TOKEN` must be set and every Host
name clients use must be allowed. `localhost` is allowed here, and
`PMAGENT_ALLOWED_HOSTS` (comma-separated) adds more. Publish the port on the
host's loopback (`-p 127.0.0.1:8000:8000`) unless TLS is in front of it.

The command is `exec`'d, so it becomes the container's main process and gets
`docker stop`'s signal. Run with `--init` (or compose's `init: true`) so a
proper init reaps child processes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
EXAMPLES = APP / "docs" / "learning" / "examples"
PORT = os.getenv("PORT", "8000")
SPRINT_REVIEW_ROOT = os.getenv("SPRINT_REVIEW_ROOT", str(APP / "data" / "sprint-review"))
# Every lesson example runs offline with the image's dependencies, except 15,
# which needs strands-agents-evals.
NEEDS_EXTRAS = {"15_eval_experiment.py"}
OFFLINE_EXAMPLES = sorted(p.name for p in EXAMPLES.glob("[0-9]*.py") if p.name not in NEEDS_EXTRAS)
COMMANDS = ("demo", "web", "cli", "smoke", "smoke-web", "sprint-review-mcp", "examples")


def _web_flags() -> list[str]:
    hosts = ["localhost", *(h.strip() for h in os.getenv("PMAGENT_ALLOWED_HOSTS", "").split(","))]
    return ["--host", "0.0.0.0", "--port", PORT,
            *(flag for h in hosts if h for flag in ("--allowed-host", h))]


def command(argv: list[str]) -> tuple[list[str], Path]:
    """(program + arguments, working directory) for `docker run <image> <argv>`."""
    name, extra = (argv[0], argv[1:]) if argv else ("demo", [])
    py = sys.executable
    if name == "demo":
        return [py, "scripts/demo_web.py", "--host", "0.0.0.0", "--port", PORT, *extra], APP
    if name == "web":
        return [py, "web.py", *_web_flags(), *extra], APP
    if name == "cli":
        return [py, "main.py", *extra], APP
    if name == "smoke":
        return [py, "scripts/smoke.py", *extra], APP
    if name == "smoke-web":
        return [py, "scripts/smoke.py", "--web", *_web_flags(), *extra], APP
    if name == "sprint-review-mcp":
        return [py, "-m", "sprint_review.server", "--root", SPRINT_REVIEW_ROOT, *extra], EXAMPLES
    return argv, APP


def run_examples() -> int:
    """Run each offline example in turn; non-zero if any fails."""
    import subprocess

    failed = [name for name in OFFLINE_EXAMPLES
              if subprocess.run([sys.executable, str(EXAMPLES / name)], cwd=APP).returncode]
    print("\nexamples failed:" if failed else "\nall examples ran.", *failed)
    return 1 if failed else 0


def main(argv: list[str]) -> None:
    if argv[:1] == ["examples"]:
        sys.exit(run_examples())
    args, cwd = command(argv)
    if argv[:1] == ["sprint-review-mcp"]:
        # An empty volume or bind mount has no sprint-review folder yet.
        Path(SPRINT_REVIEW_ROOT).mkdir(parents=True, exist_ok=True)
    os.chdir(cwd)
    try:
        os.execvp(args[0], args)
    except FileNotFoundError:
        sys.exit(f"Unknown command {args[0]!r}. Commands: {', '.join(COMMANDS)}, "
                 "or any program in the image (e.g. bash).")


if __name__ == "__main__":
    main(sys.argv[1:])
