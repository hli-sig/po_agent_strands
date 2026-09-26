# 17. A decision model beside your LLM (Jev)

> **Part 3: Extend (guided build).** It needs lesson 15, because the cascade is
> **eval-gated**. It also feeds lesson 14's extraction gate. Design:
> `docs/PLAN_HARNESS_MEMORY.md` §J.
>
> ⚠ **Data governance first.** Jev (TypeSafe) is a **second external processor**.
> Every placement below sends text to it, and zero data retention is
> enterprise-only. **No placement goes live without the project owner's approval
> for that placement** (the plan's §J.0 table). Everything in this lesson is built
> and tested **offline with a fake client**. `JEV_ENABLED` stays `false`.

## The concept

Some decisions an LLM makes are really **classification**: which lane is this?
Did the user say something durable? Is this line an instruction to the AI? An
LLM is slow and expensive at these, and its "confidence" isn't calibrated.

**Jev** (`jev-1.13.0`, via `typesafe-sdk==0.7.1`) is a *decision model*. You
send it a **state** (text) and a typed question, and get a **calibrated** answer:

| Primitive | Returns | Used for |
|-----------|---------|----------|
| `Noul` | a yes/no probability (the criteria keys are `"true"`/`"false"`) | "is this durable?" |
| `Choice` | a label, a probability per option, and `confidence` | "which route?" |
| `Score` | a rubric level with `confidence` | eval rubrics |

It costs $0.042 per million *input* tokens (output is free). It has a 64k
context, and takes text only. In a probe on 14 synthetic messages, it answered
in about 326 ms, against 869 ms for the LLM router.

**What it can't do** (from its docs) is a **design constraint**, not a footnote:
- it reads literally;
- no maths, counting or date comparison;
- large irrelevant state hurts it;
- **adversarial content can steer it**;
- **it can't generate text**.

So Jev is **never** a Strands `Model`. It is a **decision function** you call
from four seams: the router, the memory pipeline, an eval judge, and (optionally)
the approval path. It **never authorises a write**.

## The pattern: a confidence-gated cascade

```
message ─► continuation fast path? ─► yes → keep the route (no model call)
              │ no
              ▼
           Jev Choice over the routes ── confidence ≥ τ (0.7) ─► use Jev's route
              │ below τ, or error / timeout / breaker open
              ▼
           LLM router ─► if Jev's confidence < τ_low AND the LLM's route is not
                         in Jev's top two → "clarify": ask the user to pick one
                         of Jev's top two; otherwise use the LLM's route
```

```bash
uv run docs/learning/examples/17_jev_cascade.py
```

It uses a **fake** client with canned probabilities, so you see all three
branches. It has no live mode.
- a confident Jev answer (`jev (1.00)`);
- a low-confidence one that falls back to the LLM (`jev->llm (0.49)`);
- one where both are unsure and disagree, so it asks the user (`clarify`).

## What you build

1. `➕ pmagent/jev.py`, **the only TypeSafe caller**.
   - `decide(...) -> JevResult | None` returns `None` on **any** exception, so
     the caller falls back.
   - It uses a lazy client (`from typesafe_sdk import TypeSafeClient, RetryPolicy`):
     `TypeSafeClient(api_key=env.TYPESAFE_API_KEY, model=env.JEV_MODEL,
     timeout=1.5, retry=RetryPolicy(max_retries=0))`. Pass `model` explicitly,
     because the SDK otherwise uses `jev-latest`, which moves. Also pass
     `max_retries=0`, because the SDK's defaults (10 s timeout, 2 retries with
     backoff) would add about 30 s per turn while TypeSafe is down.
   - The call itself (typesafe-sdk 0.7.1):

     ```python
     from typesafe_sdk import Choice, Noul

     resp = client.system_one(
         state={"user_text": text, "previous_route": previous_route,
                "previous_user_text": previous_user_text},
         questions={"route": Choice(
             instructions="Which assistant lane should handle the user's latest message?",
             criteria={"ticket": "create or change Jira tickets",
                       "sprint": "sprint status, progress, risk",
                       "query": "any other question about Jira or Confluence",
                       # ... one entry per route in RouteDecision
                       })})
     answer = resp.answers["route"]      # ChoiceAnswer: .choice, .confidence, .probabilities

     resp = client.system_one(state={"user_text": text}, questions={"durable": Noul(
         instructions="Does the user state a lasting fact or preference about themselves or their team?",
         criteria={"true": "yes, worth remembering", "false": "no, only about this moment"})})
     resp.answers["durable"].noul        # a probability in [0, 1]
     ```

     `answers` is a dict keyed by your question names. Noul criteria keys must
     be `"true"`/`"false"`; other keys are rejected.
   - **A total deadline of 2 s:** run the call on a small executor and use
     `future.result(timeout=2)`. The SDK's `timeout` applies per phase (connect,
     read, …), not in total.
   - **A circuit breaker:** after 3 consecutive failures, skip Jev for 60 s.
     Guard its state with a `threading.Lock`, because web turns share it.
2. **Env and dependency.**
   - Add `JEV_ENABLED` (default `false`), `TYPESAFE_API_KEY`,
     `JEV_MODEL=jev-1.13.0`, `JEV_ROUTE_THRESHOLD=0.7` (τ) and
     `JEV_CLARIFY_THRESHOLD=0.5` (τ_low, as in the example) to `pmagent/env.py`
     and `.env.sample`. Both thresholds are starting points; step 8 tunes them.
   - Run `uv add typesafe-sdk==0.7.1`, **pinned exactly**.
   - In `➕ tests/conftest.py`, set `JEV_ENABLED=false`.
3. **J1: the router cascade** in `pmagent/agents/router.py`'s
   `classify_with_reason`. Use the flow above. `route_reason` gains `jev`,
   `jev->llm` and `clarify`.
   - **The state sent** is only what the human typed: `{user_text,
     previous_route, previous_user_text}`. **No assistant or tool text.** This
     is the governance table's row 1.
4. **Abstention: "clarify".**
   - `PMAssistant.send(text, route=None)` gains a `route` argument that skips the
     router.
   - When the cascade says `clarify`, return the two candidates. The CLI asks
     "query or ticket?"; the web shows two chips. Then call
     `send(text, route=chosen)`.
5. **Memory placements.** Build these **during lesson 14**, which comes after
   this one in the recommended order, because they hook into its extractor.
   Only the `decide()` calls are new here:
   - **An extraction gate:** a `Noul` "durable" on `last_user_text`. Skip the
     extractor call when it isn't durable.
   - **A suggestion flag:** a `Noul` "instructs the AI". At confidence ≥ 0.9,
     *flag* the suggestion on the Save/Discard card. It flags; it doesn't block.
     A user preference is literally an instruction, and Jev reads literally.
6. **An eval judge** (lesson 15): a custom `Evaluator` (`JevRubric`), for
   **semantic** rubrics only. Report it next to one LLM judge, with their
   agreement rate. Its input is sandbox output (fake data), so it needs no owner
   approval.
7. **Events:** `jev.decided` (question, answer, confidence, model version,
   latency, `used_fallback`, `breaker_open`) in the run log and the thinking
   chain.
8. **The eval gate for turning it on.** Add a router-comparison mode to
   `evals/run.py`. It runs **at least 100 held-out routing cases** across all
   routes, under LLM-only, Jev-only and the cascade, and reports per-route recall
   with 95% confidence intervals, latency and cost. Choose τ on a **separate
   tuning split**. `JEV_ENABLED` may default to true only if the cascade is at
   least as accurate as LLM-only, **and** the owner has approved placements 1–2.

## Definition of done: the tests to write

`➕ tests/test_jev.py`, offline, with a **fake client**, or a fake `transport=`
for the SDK:

- [ ] Confident Jev → no LLM router call (the LLM's `ScriptedModel` has no
  entries left to consume).
- [ ] Low confidence → the LLM route, with reason `jev->llm`.
- [ ] Jev raises → the LLM route. The breaker opens after 3 failures, and closes
  after its cool-down (patch the clock).
- [ ] **A hanging transport → the LLM route within the 2 s deadline.**
- [ ] The request's `extensions["timeout"]` is 1.5 s per phase.
- [ ] Both uncertain and disagreeing → `clarify` with Jev's top two, and
  `send(route=…)` then skips the router.
- [ ] `JEV_ENABLED=false` → behaviour identical to before; run the old router
  tests.
- [ ] (With lesson 14.) The memory gate skips the extractor when not durable;
  the flag appears on instruction-like suggestions.
- [ ] **The state sent contains no assistant or tool text:** assert on what the
  fake client received after a turn that included a tool result.

## Where Jev does **not** go (per its docs)

- Arithmetic and dates (sprint metrics, days stuck, FY figures, counts). That is
  code.
- "Do the drafts match the named keys?", which is exact set equality. That is
  already code (`reconcile_scope`).
- Any generation: drafts, PRDs, replies or briefs.
- Image content.
- The PRD reviewer's "what's missing" list.
- **Never a reassuring "low risk" score on an approval card.** It invites a
  reflexive yes (automation bias). If you ever build approval advisories (the
  plan's J2), use negative flags only, and they need a separate owner approval
  because they send Jira-derived content.

## Traps

- **Treating Jev like an LLM.** No generation, no maths. Give it a narrow,
  literal question over a small, relevant state.
- **A slow fallback.** The default retries turn an outage into 30-second turns.
- **Threshold tuned on the test set.** Tune τ on one split, and report on
  another.
- **Sending more than the governance table says.** The state is defined in
  code, and a test pins it.

## Exercises

1. **(run)** In the example, set `TAU = 0.4`. Which message changes branch, and
   is that better?
2. **(code)** Write the "hanging transport" test first: a transport that sleeps
   5 s. Your `decide()` must return `None` in about 2 s.
3. **(think)** Why is "clarify" only used when the two routers *disagree*, rather
   than whenever Jev is unsure?

**How the original did it:** it had an empty `jev.py` and a `TYPESAFE_API_KEY`
setting in `env.py`, and nothing used either. The rewrite dropped both; this
lesson adds the key back when Jev needs it. This lesson
places Jev where its strengths fit, and keeps it out of everything else.
