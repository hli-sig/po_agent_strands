# 15. Evals: testing the agent's judgment

> **Part 3: Extend (guided build).** It needs lesson 12's `frontend` field on the
> run log, and uses lesson 9's `ScriptedModel`. Design:
> `docs/PLAN_HARNESS_MEMORY.md` §5 ("Phase H2").

## The concept

Lesson 9's tests prove the **code** around the model is right: the gate pauses,
lanes share history, and PRDs render. They say nothing about whether the **model**
routes "how is sprint 31?" to the sprint lane, or whether it declares `scope`
when the user names three tickets. That is judgment, and judgment needs
**evals**:

| | Tests (`tests/`, lesson 9) | Evals (this lesson) |
|-|----------------------------|---------------------|
| Checks | code | the model's choices |
| Model | `ScriptedModel` | tier 1: scripted replays of known incidents; tier 2: the **real** model |
| Result | pass/fail, deterministic | a score, which may vary per run (`--repeat N`) |
| Runs | every `pytest -q` | tier 1 in `pytest`; tier 2 **on demand only** (it costs money) |

**Check properties, not wording.** "The route is `sprint`", "the pending create's
`scope` equals the three keys the user named" and "nothing was written" are
stable. "The reply says 62%" is not.

## The Strands mechanism: `strands-agents-evals`

The official package is **`strands-agents-evals`** (1.4.0).
⚠ **`strands-evals` is a different, unofficial package** that looks like a
typosquat. Pin the exact name.

The shape is **cases → a task → evaluators → a report**:

```python
from strands_evals import Case, Experiment
from strands_evals.evaluators import Evaluator, ToolCalled
from strands_evals.types import EvaluationData, EvaluationOutput

def task(case: Case) -> dict:
    ...                                   # run the agent once
    return {"output": {"reply": reply, "route": route},   # the TaskOutput shape:
            "trajectory": requested_tool_names}           # other keys are silently dropped

class RouteIs(Evaluator):
    def evaluate(self, data: EvaluationData) -> list[EvaluationOutput]:
        want, got = data.metadata["route"], data.actual_output["route"]
        return [EvaluationOutput(score=float(got == want), test_pass=got == want, reason=...)]

report = Experiment(cases=[Case(name=..., input=..., metadata={"route": "sprint"})],
                    evaluators=[RouteIs(), ToolCalled("get_sprint_status_by_number")]
                    ).run_evaluations(task)
```

It isn't a project dependency yet, so run the example with an overlay:

```bash
uv run --with strands-agents-evals==1.4.0 docs/learning/examples/15_eval_experiment.py
```

`run_evaluations` returns **one** `EvaluationReport` for the whole experiment.
It has a row per (evaluator, case) with a pass and a reason (`report.cases`,
`report.test_passes`, `report.reasons`), and one `overall_score`, here `1.0`. The package pulls in `boto3` and `strands-agents-tools`,
which is why it will be a **dev** dependency only.

## What you build

1. **The sandbox first.** Evals drive the *real* `PMAssistant`, so nothing may
   reach Jira. Create `➕ evals/sandbox.py::install()`, shared by both tiers. It
   must:
   1. **Blank `JIRA_*` and `CONFLUENCE_*` in `os.environ` before the first
      `pmagent` import.** `pmagent/env.py` runs `load_dotenv()` at import, and
      dotenv doesn't override variables that are already set.
   2. Replace the two module-global clients,
      `pmagent/tools/jira/client.py::_client` and
      `pmagent/tools/confluence_tools.py::_client`, with **recording fakes**.
      Every write method appends to `fake.writes`.
   3. Install a deny-all HTTP guard. Factor
      `scripts/smoke.py::allowed`'s wrapper out into
      `➕ pmagent/httpguard.py::install(allow=frozenset())`, with no side effects
      on import, and make `smoke.py` use it too.
   4. Force `LUCID_MCP_ENABLED=false` and the run log's `frontend="eval"`.
      Once lesson 14 exists, also force `PMAGENT_MEMORY=off`.
2. **Tier 1: `➕ tests/evals/`**, in the normal pytest run, scripted.
   - Replay known incidents through the real loop with the sandbox as a
     fixture. The fixture **restores** `requests.Session.request` and both
     `_client` globals afterwards, so it can't leak into `test_jira_tools`.
   - Seed cases:
     - a create with three named keys and no `scope` → the warning fires;
     - "sprint 31" means the sprint *number*, not id 31 → the right tool;
     - the typo "conifrm creation" after a ticket draft. It is **not** a
       fast-path continuation (`is_continuation` is False, by design), so assert
       that the classifier's prompt contains the previous route and the draft
       context. Whether the real model then keeps `ticket` is a **tier 2**
       case.
   - `NoWriteWithoutApproval` asserts `fake.writes == []` after `send()`.
3. **Tier 2: `➕ evals/run.py`**, the live model, run on demand
   (`uv add --dev strands-agents-evals==1.4.0`).
   - The task builds `PMAssistant(model=llm.build_model())` **inside the
     sandbox**. It seeds prior messages and `previous_route` from the case, for
     continuation cases.
   - It calls `send()` once, and **stops at the first approval. It never
     resumes.**
   - It returns `{"output": {"reply", "route", "route_reason", "pending",
     "usage"}, "trajectory": [requested tool names, gated ones included],
     "environment_state": [{"name": "writes", "state": fake.writes}]}`.
   - Evaluators: `RouteIs`; `ScopeDeclared` (with at least 3 user-named keys, the
     pending create's `scope` equals them); `NoWriteWithoutApproval`;
     `ToolCalled`; and at most one LLM judge.
   - Cases live in `➕ evals/cases/*.jsonl`: the three incidents plus about 25
     routing cases.
   - `--repeat N` shows flakiness. Reports go to `evals/reports/`, which is
     gitignored.
4. **Record the biases** in the report header: the judge is the same model
   family as the agent, and judge tokens aren't in `EvaluationReport`, so count
   them yourself.
5. (Later) `➕ scripts/runlog_to_case.py` turns a lesson 12 run-log turn into a
   **draft** case, with names and keys redacted, for a human to review.

## Definition of done: the tests to write

- [ ] **The sandbox blocks HTTP:** after `install()`, a `requests.post` raises.
  After the fixture ends, it doesn't.
- [ ] **The fakes record writes:** calling `add_jira_comment` directly under the
  sandbox appends to `fake.writes`.
- [ ] **One offline `Experiment`** runs end to end with a `ScriptedModel` task,
  inside `pytest`. That is example 15, wrapped as a test, and it needs the dev
  dependency.
- [ ] **The tier-1 incident cases** pass.
- [ ] `uv run evals/run.py --help` works with no network. The live run itself is
  yours to try, once, with a small case file. It is **sandboxed**, so it can't
  write to Jira, but it spends LLM tokens.

## Traps

- **Other keys in the task's return value vanish.** Only the `TaskOutput` keys
  reach evaluators. Put your extras inside `output`.
- **A sandbox that's installed too late.** If any `pmagent` module was imported
  before you blanked the env, the loaded `pmagent/env.py` module already holds your real Jira URL.
  Also patch its attributes (`env.JIRA_BASE_URL` and the rest), and test it.
- **`NoWriteWithoutApproval` as a tautology.** Without recording fakes, "no
  writes happened" is always true. The fakes make it a real check.
- **Resuming in an eval.** Approving would run the write, even against a fake.
  Tier 2 measures judgment up to the approval, and nothing after it.

## Exercises

1. **(run)** Change example 15's case to `metadata={"route": "query"}` and see
   `RouteIs` fail with its reason.
2. **(code)** Add a `StateEquals`-style evaluator that reads
   `environment_state` and fails if `writes` is non-empty.
3. **(think)** Why should the eval's case set be **held out** from any prompt
   tuning you do? (Lesson 17 needs this for choosing Jev's threshold.)

**How the original did it:** it had no evals. Its harness doc called the missing
layer "harness D", and listed "does the agent pass `scope`" as the outstanding
eval.
