# Skill: Writing a Product Requirements Document (PRD)

This skill encodes *how to write and review a good PRD*. It is loaded into the
Writer and Reviewer agents' prompts so the domain knowledge lives in one
editable place, not buried in code. The v1 structure follows Atlassian's
"Product requirements" Confluence template (see `template.md`).

> **Company-conventions seam:** before drafting, the Writer calls
> `retrieve_company_context(topic)`. Today that returns nothing (or any files
> you drop in `sample_data/company_docs/`). Wire it to your Confluence / doc
> store later and the Writer will ground PRDs in your house style automatically
> — no prompt changes needed.

## When to use

Use when the user supplies a requirement, a feature idea, or meeting notes and
wants them turned into a structured requirements document.

## PRD sections (Atlassian template)

1. **Overview** — title, target release, owner, stakeholders, status.
2. **Objective** — 2–4 sentences: what we're building and why it matters.
3. **Background / context** — the problem, prior art, relevant systems.
4. **Success metrics** — each a Goal paired with a measurable Metric.
5. **Assumptions** — what must hold true for this to work.
6. **Requirements** — the core: each a user story
   ("As a <role>, I want <capability>, so that <benefit>") with an importance
   (High/Medium/Low) and notes. Link a Jira key if one exists.
7. **User interaction & design** — UX notes or links (optional).
8. **Open questions** — unresolved points, with answers when known.
9. **Out of scope** — what we are explicitly NOT doing (prevents scope creep).

## Writing principles

- **Capture every requirement in the source.** A meeting note like "finance
  also needs the prior-period rates fixed" is a requirement — don't drop it.
- **User stories, not feature lists.** State the role and the benefit, not just
  the mechanism.
- **Metrics must be measurable.** "Works well" is not a metric; "EOM FX variance
  < 0.1% vs source" is.
- **Surface ambiguity as an Open Question** rather than inventing an answer.
- **Prefer the source's own terms** (system names, table names, acronyms).

## Reviewer checklist (used by the Reviewer agent)

Mark the PRD **approved only if all** of these hold:

1. **Coverage** — every distinct requirement implied by the source notes appears
   in the Requirements section. List any that are missing.
2. **Testability** — every success metric is measurable.
3. **Clarity** — user stories name a role and a benefit; no vague verbs.
4. **No invention** — nothing material asserted that the source didn't support
   (genuine gaps belong in Open Questions, not guessed answers).
5. **Scope** — anything the source excluded is reflected in Out of Scope.

If any check fails, set `approved=false`, list the specific gaps in
`missing_requirements` / `issues`, and give concrete `revision_notes`.
