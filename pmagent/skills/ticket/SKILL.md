# Skill: Writing a Jira ticket

This skill encodes *what a well-formed Jira issue looks like here*. It is loaded
into the Ticket Agent's prompt so the standard lives in one editable place, not
buried in Python. It follows Atlassian's own guidance on issue types, user
stories, and acceptance criteria, narrowed to this team's conventions.

> **Company-conventions seam:** the same `retrieve_company_context(topic)` stub
> the PRD skill uses is available here. Today it returns nothing. Wire it to
> Confluence later and drafts will ground themselves in house style with no
> prompt change.

## When to use

Use whenever the user wants work captured in Jira — one ticket or a whole
batch broken out of a requirement, an epic, or a PRD.

## Issue type hierarchy

Atlassian's standard hierarchy, and what each level means here:

| Type | Use for | Parent |
|---|---|---|
| **Epic** | A body of work spanning multiple sprints, delivering one outcome | — |
| **Story** | User-visible capability deliverable in a single sprint | Epic |
| **Task** | Necessary work with no direct user-facing behaviour (migration, config, spike follow-up) | Epic |
| **Bug** | Behaviour that differs from documented or intended behaviour | Epic |
| **Spike** | Time-boxed investigation whose deliverable is a decision or an estimate, not shipped code | Epic |
| **Sub-task** | A slice of one Story/Task too small to stand alone | Story or Task |

Rules:

- Never create a Sub-task without a `parent_key`.
- A Story that cannot be finished inside one sprint is an Epic — split it.
- A Spike must state its time-box and the question it answers. If you can't
  write the question, it's a Story.

## Summary

- Action-oriented, imperative, ≤ 80 characters.
- No ticket-type prefixes (`[BUG]`) — the issue type field already says that.
- Specific enough to be understood in a backlog list with no description open.

Good: `Backfill historical FX rates from RBA monthly dataset`
Bad: `FX rates` · `Fix the thing in the pipeline` · `[STORY] FX work`

## Description

Stories use the user-story form. Everything else uses context → scope → notes.

```
As a <role>, I want <capability>, so that <benefit>.

## Context
Why this matters now. The problem, not the solution.

## Scope
What is included. Bullet the concrete deliverables.

## Out of scope
What is explicitly excluded, when there's a plausible reading that includes it.

## Technical notes
Systems, tables, endpoints, constraints. Links to the PRD or design.
```

Markdown headings, bullets, numbered lists, `inline code`, **bold**, and fenced
code blocks all render correctly in Jira. Use them.

## Acceptance criteria

The most-skipped and most-valuable field. Every Story and Bug needs them.

- Write in **Given / When / Then** form where behaviour is conditional.
- Otherwise write a flat list of testable assertions.
- Each one must be verifiable by someone who did not write the ticket.
- Cover the failure path, not just the happy path.

Good: `Given a month with no published RBA rate, when the backfill runs, then the prior month's rate is carried forward and the row is flagged 'carried'.`
Good: `EOM FX variance vs source is < 0.1% for every month in 2024.`
Bad: `Works correctly` · `Rates are right` · `Tested`

## Estimation

- Story points on the Fibonacci scale: 1, 2, 3, 5, 8, 13.
- 13 is a smell — split it before creating.
- Estimate complexity and uncertainty, not hours.
- Leave `story_points` null rather than guessing when the work is genuinely
  unknown; that's a signal, and a Spike may be the right answer instead.

## INVEST check

Before proposing a Story, confirm it is:

- **I**ndependent — deliverable without waiting on a sibling ticket
- **N**egotiable — describes the need, not a locked-in implementation
- **V**aluable — a stakeholder can say why they want it
- **E**stimable — the team could put a number on it
- **S**mall — fits comfortably in one sprint
- **T**estable — the acceptance criteria above are real ones

If a Story fails **S**, split it. If it fails **T**, the acceptance criteria
aren't specific enough yet.

## Definition of Ready

A ticket is ready to create when it has: a summary, a description with context,
acceptance criteria (Story/Bug), an issue type, a parent where the hierarchy
requires one, and either an estimate or a stated reason there isn't one.

## Splitting a requirement into several tickets

When a requirement clearly contains multiple deliverables, propose the whole
set at once rather than one ticket at a time. Split along:

- **User-visible outcomes** — one Story per capability a stakeholder would ask
  for separately. This is the default.
- **Workflow steps** only when each step independently delivers value.
- **Data boundaries** — per source system, per entity — when the work genuinely
  differs between them.

Do *not* split by job function (one ticket for "backend", one for "frontend",
one for "testing"). That produces tickets that cannot ship independently and
fails INVEST's **I** and **V**.

Set `parent_key` on every ticket in a batch that belongs to a known Epic, and
keep labels and components consistent across the batch.
