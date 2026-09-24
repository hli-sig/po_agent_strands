"""All Jira I/O: `JiraClient`, the lazy `get_client()` accessor, and the one
resolution helper that needs a live lookup.

This is the only module in the package that touches the network. Everything it
depends on — field mapping, JQL, matching, rendering — is pure and imported
from below it, so the dependency graph only ever points one way and the pure
modules stay importable with no credentials at all.

Key implementation notes:
- Issue search uses Jira Cloud enhanced JQL search: /rest/api/3/search/jql
- Sprint reporting accepts the user-visible Supply Chain Sprint number, then
  resolves it to Jira's hidden internal sprint ID.
- Story points are read from JIRA_STORY_POINTS_FIELD, defaulting to Sigma's
  "Story point estimate" field: customfield_10052.
- Every call talks to a real Jira Cloud instance. There is no offline mode:
  `JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN` must be set. Tests inject a
  fake transport rather than switching this module into a fake mode.
"""

from __future__ import annotations

from typing import Any

import requests

from pmagent import env
from pmagent.tools.adf import to_adf
from pmagent.tools.adf import to_text as adf_to_text
from pmagent.tools.jira.fields import (
    build_issue_fields,
    build_issue_update_fields,
    get_flagged_field,
    get_story_points_field,
    jira_read_fields,
)
from pmagent.tools.jira.jql import JqlBuilder
from pmagent.tools.jira.matching import pick_assignee, pick_transition
from pmagent.tools.jira.metrics import days_in_current_status
from pmagent.tools.jira.render import format_candidates, format_transitions

# Jira's bulk create endpoint accepts at most 50 issues per request.
_BULK_CHUNK_SIZE = 50

# The Agile sprint/backlog move endpoints accept at most 50 issues per request.
_MOVE_CHUNK_SIZE = 50


def _response_error(resp: Any) -> str:
    """Pull a human-readable message out of a failed Jira response."""
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 - Jira sometimes returns HTML or nothing.
        data = None

    if isinstance(data, dict):
        messages = data.get("errorMessages") or []
        field_errors = data.get("errors") or {}
        detail = "; ".join(
            list(messages) + [f"{k}: {v}" for k, v in field_errors.items()]
        )
        if detail:
            return detail

    text = (getattr(resp, "text", "") or "").strip()
    status = getattr(resp, "status_code", "unknown")
    return text[:300] or f"Jira returned HTTP {status}."


def _succeeded(resp: Any) -> bool:
    """True when a Jira response is a success. Missing status means success."""
    return int(getattr(resp, "status_code", 200)) < 400


class JiraClient:
    """Owns all Jira I/O against Jira Cloud REST v3 + the Jira Agile API.

    Constructing this requires real credentials, so build it lazily via
    `get_client()` rather than at import time — that keeps importing this
    package (for the pure functions, or in tests) free of config.
    """

    def __init__(self) -> None:
        env.validate_jira()

        self._session = requests.Session()
        self._session.auth = (env.JIRA_EMAIL, env.JIRA_API_TOKEN)
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self._base = env.JIRA_BASE_URL.rstrip("/")
        # project key -> every sprint on every board in that project.
        self._sprint_cache: dict[str, list[dict]] = {}
        # project key -> {issue type name: {field id: field metadata}}.
        self._createmeta_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def search_issues(self, jql: str, max_results: int = 50) -> list[dict]:
        """Return issues matching a JQL query as simplified flat dicts.

        Convenience wrapper around `search_issues_page` for callers that don't
        care whether the result was truncated.
        """
        issues, _ = self.search_issues_page(jql, max_results=max_results)
        return issues

    def search_issues_page(
        self,
        jql: str,
        max_results: int = 50,
        fields: list[str] | None = None,
        include_changelog: bool = False,
    ) -> tuple[list[dict], bool]:
        """Run a JQL search, paging until `max_results` or the result set ends.

        Returns `(issues, truncated)`. `truncated` is True when Jira still had
        more pages when we stopped — the caller must surface that, because a
        silently-cut result set turns "sprint completion" into a wrong number.

        Uses the enhanced search endpoint, which pages with an opaque
        `nextPageToken` rather than `startAt`/`total` — there is no total count
        available, which is exactly why truncation has to be reported.
        """
        # Always prefer newest Jira issues first unless the caller already
        # supplied an ORDER BY clause.
        if "ORDER BY" not in jql.upper():
            jql = f"({jql}) ORDER BY created DESC"

        requested_fields = fields if fields is not None else jira_read_fields()

        collected: list[dict] = []
        next_page_token: str | None = None
        truncated = False

        while True:
            remaining = max_results - len(collected)
            if remaining <= 0:
                # We filled the quota; check whether Jira had more to give.
                truncated = next_page_token is not None
                break

            payload: dict[str, Any] = {
                "jql": jql,
                # 100 is the endpoint's per-page ceiling.
                "maxResults": min(remaining, 100),
                "fields": requested_fields,
            }
            if include_changelog:
                # Costs a much larger response, so it's opt-in — only sprint
                # reporting needs it, to tell a stuck ticket from a fresh one.
                # Note this endpoint wants a comma-separated STRING here; a list
                # is rejected with an unhelpful "Invalid request payload" 400.
                payload["expand"] = "changelog"
            if next_page_token:
                payload["nextPageToken"] = next_page_token

            resp = self._session.post(
                f"{self._base}/rest/api/3/search/jql",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

            collected.extend(data.get("issues", []))

            next_page_token = data.get("nextPageToken")
            if data.get("isLast") is True or not next_page_token:
                break

        return [self._normalise_issue(i) for i in collected], truncated

    def get_issue(self, key: str) -> dict:
        """Get one Jira issue by issue key and return a simplified flat dict."""
        resp = self._session.get(
            f"{self._base}/rest/api/3/issue/{key}",
            params={"fields": ",".join(jira_read_fields())},
        )
        resp.raise_for_status()
        return self._normalise_issue(resp.json())

    def get_issue_detail(self, issue_key: str, max_comments: int = 5) -> dict:
        """One issue plus the two things a flat search can't carry: its
        description and its recent comments, both as readable text.

        Kept off `jira_read_fields` on purpose. Descriptions are unbounded, and
        pulling them into every search would inflate a 50-issue result by orders
        of magnitude for the many queries that only need keys and statuses. This
        is the opt-in deep read: few issues, everything about them.

        Comments come newest-last (Jira's order), so the *last* ones are the
        live conversation — which is why `max_comments` keeps the tail rather
        than the head.
        """
        resp = self._session.get(
            f"{self._base}/rest/api/3/issue/{issue_key}",
            params={"fields": ",".join([*jira_read_fields(), "description"])},
        )
        resp.raise_for_status()
        raw = resp.json()

        detail = self._normalise_issue(raw)
        detail["description"] = adf_to_text((raw.get("fields") or {}).get("description"))
        detail["comments"] = self.get_comments(issue_key, max_comments=max_comments)
        return detail

    def get_comments(self, issue_key: str, max_comments: int = 5) -> list[dict]:
        """Recent comments on an issue, newest last, bodies rendered to text.

        Returns `{"author", "created", "body", "total", "shown"}`-shaped dicts;
        `total` lets the caller say "showing 5 of 22" instead of implying the
        five it got are all there are.
        """
        resp = self._session.get(
            f"{self._base}/rest/api/3/issue/{issue_key}/comment",
            params={"maxResults": 100},
        )
        resp.raise_for_status()
        data = resp.json()

        comments = data.get("comments", [])
        total = data.get("total", len(comments))
        recent = comments[-max_comments:] if max_comments else comments

        return [
            {
                "author": (c.get("author") or {}).get("displayName", "unknown"),
                "created": (c.get("created") or "")[:10],
                "body": adf_to_text(c.get("body")),
                "total": total,
                "shown": len(recent),
            }
            for c in recent
        ]

    def get_myself(self) -> dict:
        """Return the Jira account represented by the current API token."""
        resp = self._session.get(f"{self._base}/rest/api/3/myself")
        resp.raise_for_status()
        return resp.json()

    def list_projects(self) -> list[dict]:
        """List projects visible to the current Jira API user."""
        resp = self._session.get(f"{self._base}/rest/api/3/project/search")
        resp.raise_for_status()
        data = resp.json()
        return [
            {
                "id": p.get("id"),
                "key": p.get("key"),
                "name": p.get("name"),
            }
            for p in data.get("values", [])
        ]

    def get_sprint_meta(self, sprint_id: int) -> dict:
        """Return a sprint's metadata (name, state, dates, goal).

        The one thing JQL genuinely cannot give us, so it stays on the Agile
        API.
        """
        resp = self._session.get(f"{self._base}/rest/agile/1.0/sprint/{sprint_id}")
        resp.raise_for_status()
        return resp.json()

    def get_sprint_issues(self, sprint_id: int, max_results: int = 500) -> dict:
        """Return sprint metadata plus every normalised issue in the sprint.

        Metadata comes from the Agile API; the issues come from JQL
        (`sprint = <id>`) rather than `/rest/agile/1.0/sprint/{id}/issue`. That
        keeps sprint reporting on the same code path — and the same paging and
        field handling — as every other search, and lets callers add filters
        (status category, type, assignee) without a second implementation.
        """
        sprint = self.get_sprint_meta(sprint_id)

        jql = JqlBuilder().sprint(sprint_id).order_by("status ASC, created ASC").build()
        issues, truncated = self.search_issues_page(
            jql,
            max_results=max_results,
            # Sprint health depends on knowing how long work has been parked.
            include_changelog=True,
        )

        return {"sprint": sprint, "issues": issues, "truncated": truncated}

    def list_boards(self, project_key: str | None = None) -> list[dict]:
        """List Agile boards visible to the current Jira API user."""
        params: dict[str, Any] = {"maxResults": 50}
        if project_key:
            params["projectKeyOrId"] = project_key

        resp = self._session.get(
            f"{self._base}/rest/agile/1.0/board",
            params=params,
        )
        resp.raise_for_status()

        data = resp.json()
        return [
            {
                "id": b.get("id"),
                "name": b.get("name"),
                "type": b.get("type"),
            }
            for b in data.get("values", [])
        ]

    def list_board_sprints(
        self,
        board_id: int,
        state: str = "active,future,closed",
    ) -> list[dict]:
        """List sprints for a Jira board."""
        all_sprints: list[dict] = []
        start_at = 0
        max_results = 50

        while True:
            resp = self._session.get(
                f"{self._base}/rest/agile/1.0/board/{board_id}/sprint",
                params={
                    "state": state,
                    "startAt": start_at,
                    "maxResults": max_results,
                },
            )
            resp.raise_for_status()
            data = resp.json()

            batch = data.get("values", [])
            all_sprints.extend(batch)

            if data.get("isLast") is True:
                break

            total = data.get("total")
            if total is not None:
                start_at += len(batch)
                if start_at >= int(total) or not batch:
                    break
            else:
                if len(batch) < max_results:
                    break
                start_at += len(batch)

        return [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "state": s.get("state"),
                "startDate": s.get("startDate"),
                "endDate": s.get("endDate"),
                "completeDate": s.get("completeDate"),
            }
            for s in all_sprints
        ]

    def project_sprints(self, project_key: str) -> list[dict]:
        """Every sprint across every board in a project, cached per instance.

        Enumerating sprint *names* is the one thing JQL cannot do, so this
        board walk is unavoidable — but it's several requests, so we do it once
        per project per process.
        """
        cached = self._sprint_cache.get(project_key)
        if cached is not None:
            return cached

        sprints: list[dict] = []
        for board in self.list_boards(project_key):
            for sprint in self.list_board_sprints(board["id"]):
                sprints.append({**sprint, "board_id": board["id"], "board_name": board["name"]})

        self._sprint_cache[project_key] = sprints
        return sprints

    def find_sprint_id_by_name(
        self,
        project_key: str,
        sprint_name: str,
    ) -> int:
        """Resolve user-visible sprint name to Jira's internal sprint id."""
        all_sprints = self.project_sprints(project_key)
        matches = [
            s for s in all_sprints if (s.get("name") or "").lower() == sprint_name.lower()
        ]

        if not matches:
            sample = ", ".join([s.get("name", "") for s in all_sprints if s.get("name")][:10])
            raise ValueError(
                f"No sprint found with name '{sprint_name}' in project {project_key}. "
                f"Sample visible sprints: {sample}"
            )

        # Prefer active sprint, then future, then closed.
        state_priority = {"active": 0, "future": 1, "closed": 2}
        matches.sort(key=lambda x: state_priority.get(x.get("state"), 99))

        return int(matches[0]["id"])

    def resolve_sprint(self, project_key: str, sprint_ref: int | str) -> int:
        """Resolve a user's way of naming a sprint to Jira's internal id.

        Accepts the visible number (`31` -> "Supply Chain Sprint 31", per
        `JIRA_SPRINT_NAME_TEMPLATE`) or a full sprint name. Note a bare number
        is treated as the *visible* number, never as an internal id — that
        conflation is the single most common way sprint reporting silently
        reports on the wrong sprint.
        """
        ref = str(sprint_ref).strip()
        name = env.JIRA_SPRINT_NAME_TEMPLATE.format(n=int(ref)) if ref.isdigit() else ref
        return self.find_sprint_id_by_name(project_key, name)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def create_issue(self, draft: dict) -> dict:
        """Create a single Jira issue from a TicketDraft-shaped dict."""
        payload = {"fields": build_issue_fields(draft)}

        resp = self._session.post(f"{self._base}/rest/api/3/issue", json=payload)
        resp.raise_for_status()
        return resp.json()

    def create_issues(self, drafts: list[dict]) -> list[dict]:
        """Create many issues, returning one result per draft in input order.

        Uses Jira's bulk endpoint, which caps at 50 issues per request, so
        larger batches are chunked. Jira reports per-issue failures rather than
        failing the whole request, and it returns successes and errors in two
        separate arrays — `errors[].failedElementNumber` is what maps a failure
        back to its draft. Callers get one result per draft either way, so a
        partial success is never silently reported as a total one.
        """
        results: list[dict] = []

        for start in range(0, len(drafts), _BULK_CHUNK_SIZE):
            chunk = drafts[start : start + _BULK_CHUNK_SIZE]
            chunk_results: list[dict | None] = [None] * len(chunk)

            resp = self._session.post(
                f"{self._base}/rest/api/3/issue/bulk",
                json={"issueUpdates": [{"fields": build_issue_fields(d)} for d in chunk]},
            )

            # Jira answers 400 — not a partial result — when *every* issue in the
            # chunk is rejected, but the body still carries the same `errors[]`
            # array as a partial success. `raise_for_status()` here threw that
            # body away, so a rejected field ("cannot be set. It is not on the
            # appropriate screen, or unknown") reached the user as nothing but
            # "400 Client Error: Bad Request", which names neither the field nor
            # the issue. Parse the array whatever the status; escalate only when
            # there isn't one.
            try:
                data = resp.json()
            except ValueError:
                data = None

            if not isinstance(data, dict) or (
                not _succeeded(resp) and not data.get("errors")
            ):
                raise ValueError(
                    f"Jira rejected the bulk create request: {_response_error(resp)}"
                )

            failed_positions = set()
            for error in data.get("errors", []):
                position = error.get("failedElementNumber")
                messages = (error.get("elementErrors") or {}).get("errorMessages") or []
                field_errors = (error.get("elementErrors") or {}).get("errors") or {}
                detail = "; ".join(
                    list(messages) + [f"{k}: {v}" for k, v in field_errors.items()]
                )
                if position is not None and 0 <= position < len(chunk):
                    failed_positions.add(position)
                    chunk_results[position] = {
                        "summary": chunk[position].get("summary", ""),
                        "error": detail or "Jira rejected this issue.",
                    }

            # Successes come back in input order, skipping the failed slots.
            created = data.get("issues", [])
            open_slots = [i for i in range(len(chunk)) if i not in failed_positions]
            for slot, issue in zip(open_slots, created):
                chunk_results[slot] = {
                    "summary": chunk[slot].get("summary", ""),
                    "key": issue.get("key"),
                }

            for index, result in enumerate(chunk_results):
                results.append(
                    result
                    or {
                        "summary": chunk[index].get("summary", ""),
                        "error": "Jira returned no result for this issue.",
                    }
                )

        return results

    def search_assignable_users(self, project_key: str, query: str) -> list[dict]:
        """Find users who can be assigned issues in a project.

        Uses the *assignable* search rather than the general user directory, so
        a name that matches someone without permission in this project doesn't
        come back as a false candidate.
        """
        resp = self._session.get(
            f"{self._base}/rest/api/3/user/assignable/search",
            params={"project": project_key, "query": query, "maxResults": 50},
        )
        resp.raise_for_status()
        return [
            {
                "account_id": u.get("accountId"),
                "display_name": u.get("displayName"),
                "email": u.get("emailAddress"),
                "active": u.get("active", True),
            }
            for u in resp.json()
            if u.get("accountId")
        ]

    def assign_issue(self, issue_key: str, account_id: str | None) -> None:
        """Assign an existing issue, or unassign it when `account_id` is None.

        Jira returns 204 with no body on success.
        """
        resp = self._session.put(
            f"{self._base}/rest/api/3/issue/{issue_key}/assignee",
            json={"accountId": account_id},
        )
        resp.raise_for_status()

    def update_issue(self, issue_key: str, changes: dict) -> None:
        """Edit fields on an existing issue. Jira returns 204 with no body.

        Field mapping goes through `build_issue_update_fields`, so an edit can
        only ever set fields the same way creation does. Sprint is not editable
        here — see `move_issues_to_sprint`.
        """
        fields = build_issue_update_fields(changes)
        if not fields:
            raise ValueError(f"No updatable fields supplied for {issue_key}.")

        resp = self._session.put(
            f"{self._base}/rest/api/3/issue/{issue_key}",
            json={"fields": fields},
        )
        resp.raise_for_status()

    def list_transitions(self, issue_key: str) -> list[dict]:
        """Transitions available *from this issue's current status*, flattened.

        Jira's workflow API takes a transition **id**, never a status name — and
        which ids exist depends on where the issue currently sits in the
        workflow. So this is a per-issue lookup and deliberately not cached the
        way `createmeta` is: the same project's issues offer different
        transitions depending on their own status.
        """
        resp = self._session.get(
            f"{self._base}/rest/api/3/issue/{issue_key}/transitions"
        )
        resp.raise_for_status()
        return [
            {
                "id": t.get("id"),
                "name": t.get("name", ""),
                "to_status": (t.get("to") or {}).get("name", ""),
            }
            for t in resp.json().get("transitions", [])
        ]

    def transition_issues(self, issue_keys: list[str], target: str) -> list[dict]:
        """Move issues to the `target` status. One result per key, input order.

        Per-issue by construction — each key needs its own transition lookup,
        because two issues heading for the same status from different starting
        points need different transition ids. Failures are therefore per-issue
        too, like `add_comments` and unlike `_post_move`.
        """
        results: list[dict] = []

        for key in issue_keys:
            transitions = self.list_transitions(key)
            match, ambiguous = pick_transition(transitions, target)

            if not match:
                results.append(
                    {
                        "key": key,
                        "error": self._transition_error(
                            key, target, transitions, ambiguous
                        ),
                        "to_status": None,
                    }
                )
                continue

            resp = self._session.post(
                f"{self._base}/rest/api/3/issue/{key}/transitions",
                json={"transition": {"id": match["id"]}},
            )
            results.append(
                {
                    "key": key,
                    "error": None if _succeeded(resp) else _response_error(resp),
                    "to_status": match["to_status"],
                }
            )

        return results

    def _transition_error(
        self, issue_key: str, target: str, transitions: list[dict], ambiguous: list[dict]
    ) -> str:
        """Explain an unmatched transition in terms the user can act on.

        Costs one extra GET, but only on the failure path — and "it is already
        In Review" versus "there is no route to In Review from here" are
        different problems with different fixes. Without the current status,
        both read as the same unhelpful "no transition found".
        """
        if ambiguous:
            return (
                f"'{target}' is ambiguous — {len(ambiguous)} transitions match: "
                f"{format_transitions(ambiguous)}. Ask which one, then pass its "
                f"exact transition name."
            )

        try:
            current = self.get_issue(issue_key).get("status", "")
        except Exception:  # noqa: BLE001 — a failed lookup must not mask the real error
            current = ""

        if current and current.strip().lower() == (target or "").strip().lower():
            return f"already in '{current}'; nothing to do."

        origin = f" from '{current}'" if current else ""
        return (
            f"no transition to '{target}'{origin}. "
            f"Available: {format_transitions(transitions)}"
        )

    def add_comments(self, issue_keys: list[str], comment: str) -> list[dict]:
        """Post the same comment on each issue. One result per key, input order.

        The comment body is ADF, exactly like a description — Jira rejects a
        plain string here too, so it goes through `to_adf` and gets the same
        Markdown subset (headings, bullets, bold, code).

        Jira has **no bulk-comment endpoint** — unlike creation (`/issue/bulk`)
        or sprint moves — so this is one request per issue. That makes failure
        genuinely per-issue: a 404 on one key says nothing about the others, so
        each key carries its own error instead of a whole chunk failing together
        the way `_post_move` has to report it.
        """
        body = {"body": to_adf(comment)}
        results: list[dict] = []

        for key in issue_keys:
            resp = self._session.post(
                f"{self._base}/rest/api/3/issue/{key}/comment", json=body
            )
            results.append(
                {"key": key, "error": None if _succeeded(resp) else _response_error(resp)}
            )

        return results

    def move_issues_to_sprint(self, sprint_id: int, issue_keys: list[str]) -> list[dict]:
        """Move existing issues into a sprint. One result per key, input order.

        This is the *only* supported way to re-sprint an issue that already
        exists: the sprint custom field works on create but Jira rejects it on
        the edit screen, so `update_issue` cannot do it.

        The endpoint caps at 50 issues per request and rejects a bad request
        wholesale rather than per-issue, so a failed chunk is reported against
        every key in it — never summarise a partly-failed move as done.
        """
        return self._post_move(
            f"{self._base}/rest/agile/1.0/sprint/{sprint_id}/issue", issue_keys
        )

    def move_issues_to_backlog(self, issue_keys: list[str]) -> list[dict]:
        """Pull existing issues out of whatever sprint they're in, into the backlog."""
        return self._post_move(f"{self._base}/rest/agile/1.0/backlog/issue", issue_keys)

    def _post_move(self, url: str, issue_keys: list[str]) -> list[dict]:
        """Chunked `{"issues": [...]}` POST shared by the sprint/backlog moves."""
        results: list[dict] = []

        for start in range(0, len(issue_keys), _MOVE_CHUNK_SIZE):
            chunk = issue_keys[start : start + _MOVE_CHUNK_SIZE]
            resp = self._session.post(url, json={"issues": chunk})
            error = None if _succeeded(resp) else _response_error(resp)
            results += [{"key": key, "error": error} for key in chunk]

        return results

    def get_create_metadata(self, project_key: str) -> dict:
        """Return `{issue type name: {field id: field metadata}}` for a project.

        Backs the pre-flight check in `validate_ticket_drafts`: without it, a
        bad issue type or a required field the project demands only surfaces as
        an opaque 400 at creation time.
        """
        cached = self._createmeta_cache.get(project_key)
        if cached is not None:
            return cached

        types_resp = self._session.get(
            f"{self._base}/rest/api/3/issue/createmeta/{project_key}/issuetypes",
            params={"maxResults": 100},
        )
        types_resp.raise_for_status()

        meta: dict[str, dict] = {}
        for issue_type in types_resp.json().get("issueTypes", []):
            name = issue_type.get("name", "")
            fields_resp = self._session.get(
                f"{self._base}/rest/api/3/issue/createmeta/{project_key}"
                f"/issuetypes/{issue_type.get('id')}",
                params={"maxResults": 200},
            )
            fields_resp.raise_for_status()
            meta[name] = {
                f.get("fieldId"): f for f in fields_resp.json().get("fields", [])
            }

        self._createmeta_cache[project_key] = meta
        return meta

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalise_issue(raw: dict) -> dict:
        """Flatten a raw Jira REST issue into the simple shape our code uses."""
        f = raw.get("fields", {})
        status_obj = f.get("status") or {}
        status = status_obj.get("name", "")
        # `statusCategory.key` is one of: new / indeterminate / done. Unlike the
        # status *name*, it is stable across custom workflows — a status called
        # "Released" or "Won't Do" still reports category "done".
        status_category = (status_obj.get("statusCategory") or {}).get("key", "")

        story_points_raw = f.get(get_story_points_field())
        try:
            story_points = float(story_points_raw) if story_points_raw is not None else None
        except (TypeError, ValueError):
            story_points = None

        parent = f.get("parent") or {}

        return {
            "key": raw.get("key", ""),
            "summary": f.get("summary", ""),
            "type": (f.get("issuetype") or {}).get("name", ""),
            "status": status,
            "status_category": status_category,
            "story_points": story_points,
            "assignee": ((f.get("assignee") or {}) or {}).get("displayName"),
            "priority": ((f.get("priority") or {}) or {}).get("name"),
            "parent": parent.get("key"),
            "created": f.get("created"),
            "updated": f.get("updated"),
            "resolved": f.get("resolutiondate"),
            "blocked": status.lower() in ("blocked", "impediment"),
            "flagged": bool(f.get(get_flagged_field())),
            # 0 unless the caller asked for the changelog — see days_in_current_status.
            "days_in_status": days_in_current_status(raw),
            "labels": f.get("labels", []),
            "components": [c.get("name") for c in f.get("components", [])],
        }


_client: JiraClient | None = None


def get_client() -> JiraClient:
    """Return the shared `JiraClient`, building it on first use.

    Deliberately lazy: `JiraClient.__init__` validates credentials, so building
    it at import time would make `import pmagent.tools.jira` fail without a
    configured `.env` — including for the pure modules in this package, which
    need no Jira at all.
    """
    global _client
    if _client is None:
        _client = JiraClient()
    return _client


def resolve_assignee(project_key: str, assignee: str) -> tuple[str | None, str]:
    """Resolve a name or email to an account id.

    Returns `(account_id, message)`. When `account_id` is None, `message`
    explains why and lists candidates.

    Lives here rather than in `matching.py` because it needs a live user search;
    the choosing half — which is the part with the interesting rules — is the
    pure `pick_assignee` this delegates to.
    """
    if not assignee or not assignee.strip():
        return None, "No assignee given."

    candidates = get_client().search_assignable_users(project_key, assignee)
    if not candidates:
        return None, (
            f"No user matching '{assignee}' can be assigned issues in "
            f"{project_key}. They may lack project permission, or the name may "
            f"be spelled differently in Jira."
        )

    match, alternatives = pick_assignee(candidates, assignee)
    if match:
        return match["account_id"], f"{match['display_name']}"

    return None, (
        f"'{assignee}' is ambiguous in {project_key} — {len(alternatives)} "
        f"people match. Ask the user which one, then pass their exact email or "
        f"account id:\n{format_candidates(alternatives)}"
    )
