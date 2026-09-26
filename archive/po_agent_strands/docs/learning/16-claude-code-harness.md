# 16. The harness around you: Claude Code config

> **Part 3: Extend (guided build).** It is independent of lessons 12–15, and quick
> (2–3 hours). Design: `docs/PLAN_HARNESS_MEMORY.md` §6 ("Phase A").

## The concept

"Harness" means four different things around this project. The original
project's harness doc named them, and this course uses the same map:

| | Harness | What it steers | Lesson |
|-|---------|----------------|--------|
| **A** | Claude Code's config for this repo (`.claude/`) | **Claude, working on the code** | this one |
| **B** | the app itself: loop, lanes, gate, run log | the LLM inside PO Agent | 5, 6, 12 |
| **C** | `tests/` | the deterministic code | 9 |
| **D** | `evals/` | the agent's judgment | 15 |

Lessons 12–15 and 17 extend B (the app) and build D (evals); in the recommended
order you've done 12 and 13 so far. This lesson is **A**: if you use Claude Code (or any
coding agent) on this repo, it should run the right tests without being asked,
and it should **never** run the app against the production Jira by accident.

The rule for what belongs in A comes from the original harness doc: **a harness
item earns its place only if it does something tests can't.** Tests can't make
Claude run them at the right moment, and they can't stop a live `main.py`. Hooks
and permissions can.

## The mechanism: hooks, permissions, skills and subagents

It all lives in `.claude/` at the repo root, and you commit it. Claude Code
reads `.claude/settings.json`.

**Hooks** run a shell command on a Claude Code event. A matcher matches **tool
names**, so path and command filtering happens *inside* your script, which reads
the event JSON on stdin:

```json
{
  "hooks": {
    "PostToolUse": [
      {"matcher": "Edit|Write",
       "hooks": [{"type": "command",
                  "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/after_edit.sh"}]}
    ]
  }
}
```

```bash
#!/usr/bin/env bash
# .claude/hooks/after_edit.sh: fast checks after Claude edits agent code
path=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input", {}).get("file_path", ""))')
case "$path" in
  */pmagent/tools/*|*/pmagent/agents/*|*/pmagent/web/*)
    cd "$CLAUDE_PROJECT_DIR" && uv run pytest tests/test_agent_lanes.py tests/test_tool_specs.py -q >&2 || exit 2 ;;
  */docs/learning/*.md)
    cd "$CLAUDE_PROJECT_DIR" && uv run scripts/build_course.py >&2 || exit 2 ;;
esac
exit 0
```

Exit code 2 feeds stderr back to Claude as something to fix. (On a `PreToolUse`
hook, exit 2 also **blocks** the tool call.)

**Permissions** decide which Bash commands run without asking (`allow`), which
always ask (`ask`), and which are refused (`deny`).

**Skills** (`.claude/skills/<name>/SKILL.md`) are reusable instructions. With
`disable-model-invocation: true` in the front-matter, only a human can run
one, as `/name`.

**Subagents** (`.claude/agents/<name>.md`) are named reviewers with their own
brief and tool list.

Check the exact current schema with Claude Code's docs, or its `update-config`
skill, before you write the files. The shape above is what the plan was checked
against.

## What you build

1. **Hooks** (`➕ .claude/settings.json`, `➕ .claude/hooks/`):
   - `PostToolUse` on `Edit|Write`:
     - agent/tool/web code → the two fast structural tests (the script above);
     - `docs/learning/*.md` → rebuild the course (`tests/test_course_build.py`
       fails otherwise). This one only applies in a copy of this repository;
       your `$MY` rebuild has no course. Skip it there.
   - `PreToolUse` on `Bash`: if the command contains `git commit`, run the full
     `uv run pytest -q`, and exit 2 on red. **Time it once** and write the time in
     a comment, because it includes uvicorn and Playwright tests.
2. **Permissions:**
   - `allow`: `Bash(uv run pytest:*)`, `Bash(uv run scripts/demo_web.py:*)`,
     `Bash(uv run scripts/check_claude_config.py:*)` and
     `Bash(uv run docs/learning/examples/:*)`;
   - `ask`: `scripts/smoke.py`. It reads production Jira and spends money. (In
     this reference repository the same rule also covers `scripts/web_drive.py`
     and `scripts/ui_screenshots.py`, and `allow` adds `scripts/build_course.py`
     and `scripts/verbatim_report.py`; your rebuild has none of those scripts.)
   - `deny`: `Bash(uv run main.py:*)` and `Bash(uv run web.py:*)`.
   - Commands that **contain** `PMAGENT_ALLOW_LIVE` or `env -u CLAUDECODE`
     anywhere: permission rules match by prefix, so they can't express
     "contains" reliably. Refuse these in the `PreToolUse` `Bash` hook instead:
     if the command contains either string, exit 2 with a reason.
3. **An accident guard in code:** `➕ pmagent/liveguard.py::refuse_under_claude()`.
   - Claude Code sets `CLAUDECODE=1` in its shells. When that is set, and
     `PMAGENT_ALLOW_LIVE=1` is not, print why and `sys.exit(3)`.
   - Call it **only** in the `if __name__ == "__main__":` blocks of `main.py`
     and `web.py`, **before** anything is built. Never call it in `main()`:
     tests import `main` and call `main()`, and `scripts/smoke.py` calls
     `main.main()` in-process, so both are unaffected.
   - In `➕ tests/conftest.py`, **remove `CLAUDECODE`** for every test. Claude Code
     runs pytest with it set.
   - It is honest about its limit: `uv run python -c "import main; main.main()"`
     bypasses it. It stops *accidents*, not intent. Defence in depth, with the
     permission `deny`.
4. **A skill**, `➕ .claude/skills/audit-jira-config/SKILL.md`, set to
   `disable-model-invocation: true`: a read-only audit of Jira fields and create
   metadata, run **through `scripts/smoke.py`'s guard**.
5. **Three subagents** (`➕ .claude/agents/`):
   - `plan-reviewer`: reviews a plan before code. It codifies the **plan →
     review → implement → evidence** workflow this project was built with.
   - `course-tester`: follows the course as a learner. Offline only; reverts its
     experiments; reports, never fixes.
   - `gate-auditor`: is each write's approval text **sufficient** for a human to
     say yes? Its rule: a finding seen three times becomes a test.
6. `➕ scripts/check_claude_config.py`: validates `.claude/settings.json` as JSON,
   and checks every script a hook references exists. Add a test that runs it.

## Definition of done

- [ ] `➕ tests/test_liveguard.py`, in three parts:
  - **The refusal:** run `main.py` as a subprocess with `CLAUDECODE=1`,
    `PMAGENT_ALLOW_LIVE` unset, the `JIRA_*`/`CONFLUENCE_*` variables set to
    `""` (dotenv never overrides them), `stdin=subprocess.DEVNULL` and a
    `timeout`. It
    exits with code 3 and prints the message. (Calling `main.main()` directly
    can't test this: the guard sits in `__main__`. The `DEVNULL` stdin makes a
    broken guard end at the first prompt instead of waiting for input.)
  - **The allowed paths, as unit tests of `refuse_under_claude()` itself.** With
    `CLAUDECODE` unset, it returns `None`. With `CLAUDECODE=1` and
    `PMAGENT_ALLOW_LIVE=1`, it returns `None`. **Never test the allowed path by
    running `main.py`:** a stub on `main.main` doesn't reach a `run_path` or
    subprocess run (they load the file afresh), so the real CLI would start
    against your `.env`.
  - **The wiring:** parse `main.py` and `web.py` with `ast`, and assert that the
    `if __name__ == "__main__":` block calls `refuse_under_claude()` before any other call (an import
    line before it is fine).
- [ ] `➕ tests/conftest.py` removes `CLAUDECODE`, so `uv run pytest -q` gives the
  same result inside and outside Claude Code. Only the refusal test sets it
  again, with `monkeypatch`.
- [ ] `uv run scripts/check_claude_config.py` passes, and its test passes.
- [ ] **(manual)** In Claude Code, ask it to edit a docstring in
  `pmagent/tools/finance_tools.py`. The hook runs the two structural tests.
  Then ask it to "run main.py": it is denied, and the reason is shown.

## Traps

- **Guarding `main()` instead of `__main__`** breaks the CLI tests and
  `smoke.py`, the safe launcher. That is the wrong way round.
- **A slow `PostToolUse` hook on every edit.** Only the two fast structural
  tests run per edit. The full suite runs before commit.
- **A deny rule is not a sandbox.** Say so in the docs, as the plan does.
- **`CLAUDECODE` leaking into pytest** makes results depend on where you ran
  them. The "allowed with `PMAGENT_ALLOW_LIVE`" test could pass for the wrong
  reason, and any subprocess test of `__main__` would exit 3. Clear it in
  conftest, and set it only in the refusal test.

## Exercises

1. **(code)** Write `refuse_under_claude()` and its test before touching
   `main.py`. Watch the test fail, then wire it into `__main__`.
2. **(think)** Which of this lesson's items could a *test* have done instead?
   (Hint: none. That is the rule.)
3. **(think)** The `ask` list includes `scripts/smoke.py`, even though it
   blocks writes. Why? (Reads of production data, and model spend.)

**How the original did it:** its harness doc listed these items. This project
built them only as reviewer briefs, spawned by hand. This lesson turns them into
config.
