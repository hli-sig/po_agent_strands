"""Unit tests for the Jira layer.

No network and no credentials. The pure functions are tested on plain dicts;
client paging is tested by injecting a fake session into a `JiraClient` built
with `__new__`, which skips the credential check in `__init__`. Same spirit as
`FakeWorkbook` in `test_spreadsheet_tools.py`: inject the boundary, don't add a
mode flag to production code.
"""

import pytest

from pmagent.schemas import TicketDraft
from pmagent.tools.jira import (
    JiraClient,
    JqlBuilder,
    bucket_of,
    build_issue_fields,
    build_issue_update_fields,
    check_draft_standard,
    compute_sprint_metrics,
    format_candidates,
    format_issue_line,
    get_flagged_field,
    get_sprint_field,
    get_story_points_field,
    is_blocked,
    is_done,
    parse_drafts,
    pick_assignee,
    pick_transition,
    reconcile_scope,
    render_issue_detail,
    render_sprint_report,
)

# The two private renderers are pulled from their own module rather than the
# package: `__init__` re-exports the public surface, and a test that pokes at
# internals should say so by reaching for the module that owns them.
from pmagent.tools.jira.render import _clip, _render_batch_result


# ---------------------------------------------------------------------------
# The package surface
# ---------------------------------------------------------------------------
def test_the_package_exports_exactly_what_all_advertises():
    """`__init__` lists every re-export twice — once to import it, once in
    `__all__` — and two lists that must agree are two lists that can drift.
    This is the cheap way to keep them honest: anything importable and public
    must be advertised, and anything advertised must actually be there.
    """
    import pmagent.tools.jira as pkg

    public = {
        name for name in vars(pkg)
        if not name.startswith("_") and not isinstance(vars(pkg)[name], type(pkg))
    }
    assert public == set(pkg.__all__)


# The tools' parameter names are the contract the model, the approval prompt
# (`cli/approval.py`) and the web UI all read. Later tests call the tools by
# these names, so they are pinned here, where the tools are written.
TOOL_PARAMETERS = {
    "query_jira_issues": ["jql", "max_results"],
    "read_jira_issues_by_key": ["keys"],
    "search_jira_issues": ["jql"],
    "get_sprint_status": ["sprint_id"],
    "get_sprint_status_by_number": ["sprint_number"],
    "find_jira_user": ["name_or_email", "project_key"],
    "read_jira_issue_details": ["issue_keys", "max_comments"],
    "list_jira_transitions": ["issue_key"],
    "validate_ticket_drafts": ["drafts"],
    "create_jira_issues": ["drafts", "scope"],
    "create_jira_issue": ["summary", "description", "issue_type", "story_points", "labels",
                          "components", "acceptance_criteria"],
    "assign_jira_issue": ["issue_key", "assignee"],
    "update_jira_issue": ["issue_key", "summary", "description", "acceptance_criteria",
                          "story_points", "labels", "components", "priority", "parent_key",
                          "issue_type"],
    "add_jira_comment": ["issue_keys", "comment"],
    "transition_jira_issues": ["issue_keys", "status"],
    "move_jira_issues_to_sprint": ["issue_keys", "sprint"],
    "remove_jira_issues_from_sprint": ["issue_keys"],
}


def test_every_jira_tool_has_the_parameters_later_steps_call_it_with():
    import pmagent.tools.jira as pkg

    tools = {t.tool_name: t for t in [*pkg.READ_TOOLS, *pkg.WRITE_TOOLS]}
    assert set(tools) == set(TOOL_PARAMETERS)
    for name, params in TOOL_PARAMETERS.items():
        assert sorted(tools[name].tool_spec["inputSchema"]["json"]["properties"]) == sorted(params), name


def test_the_client_methods_the_web_demo_fakes():
    # The web demo and tests replace the client with a fake that has these methods.
    for method in ("search_issues_page", "list_transitions", "add_comments", "transition_issues",
                   "move_issues_to_sprint", "move_issues_to_backlog", "create_issues"):
        assert callable(getattr(JiraClient, method)), method


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    """Returns queued payloads and records every request made.

    Queue a `FakeResponse` directly to control the status code (the move
    endpoints report failure by status, not by body).
    """

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.posts = []
        self.gets = []
        self.puts = []

    def _next(self):
        item = self._payloads.pop(0)
        return item if isinstance(item, FakeResponse) else FakeResponse(item)

    # **kwargs: a client may pass `timeout=` (it should); the fake ignores it.
    def post(self, url, json=None, **kwargs):
        self.posts.append((url, json))
        return self._next()

    def get(self, url, params=None, **kwargs):
        self.gets.append((url, params))
        return self._next()

    def put(self, url, json=None, **kwargs):
        self.puts.append((url, json))
        return self._next()


def make_client(payloads):
    client = JiraClient.__new__(JiraClient)
    client._session = FakeSession(payloads)
    client._base = "https://example.atlassian.net"
    client._sprint_cache = {}
    return client


def raw_issue(key, status, category, points=None, flagged=False):
    # Custom field ids are instance config, so ask `env` for them rather than
    # hardcoding — a fixture pinned to one tenant's ids fails on every other.
    return {
        "key": key,
        "fields": {
            "summary": f"summary for {key}",
            "status": {"name": status, "statusCategory": {"key": category}},
            "issuetype": {"name": "Story"},
            "labels": [],
            "components": [],
            get_story_points_field(): points,
            get_flagged_field(): {"value": "Impediment"} if flagged else None,
            "assignee": {"displayName": "Han Li"},
            "priority": {"name": "High"},
            "created": "2026-07-01T09:00:00.000+1000",
        },
    }


def issue(key, status="To Do", category="new", points=0, **extra):
    return {
        "key": key,
        "summary": f"summary for {key}",
        "type": "Story",
        "status": status,
        "status_category": category,
        "story_points": points,
        **extra,
    }


# ---------------------------------------------------------------------------
# JqlBuilder
# ---------------------------------------------------------------------------
def test_builder_composes_clauses_and_order_by():
    jql = JqlBuilder().project("CSCI").sprint(1234).build()
    assert jql == 'project = "CSCI" AND sprint = 1234 ORDER BY created DESC'


def test_builder_status_category_negation():
    jql = JqlBuilder().project("CSCI").status_category("Done", negate=True).build()
    assert 'statusCategory != "Done"' in jql


def test_builder_escapes_quotes_in_literals():
    jql = JqlBuilder().text('say "hi"').build()
    assert r'text ~ "say \"hi\""' in jql


def test_builder_rejects_empty_query():
    with pytest.raises(ValueError):
        JqlBuilder().build()


def test_builder_sprint_takes_internal_id_as_int():
    assert "sprint = 42" in JqlBuilder().sprint("42").build()


# ---------------------------------------------------------------------------
# Completion semantics — status category beats status name
# ---------------------------------------------------------------------------
def test_custom_done_status_name_still_counts_as_done():
    # "Released" is not in any hardcoded done-name list, but its category is.
    assert is_done(issue("CSCI-1", status="Released", category="done"))


def test_wont_do_counts_as_done_not_as_outstanding():
    assert is_done(issue("CSCI-2", status="Won't Do", category="done"))


def test_falls_back_to_status_name_without_a_category():
    assert is_done({"key": "CSCI-3", "status": "Done"})
    assert not is_done({"key": "CSCI-4", "status": "In Progress"})


def test_done_issue_is_never_blocked():
    stale = issue("CSCI-5", status="Done", category="done", days_in_status=99)
    assert not is_blocked(stale)


def test_flagged_issue_is_blocked():
    assert is_blocked(issue("CSCI-6", flagged=True))


def test_buckets_are_mutually_exclusive():
    assert bucket_of(issue("A", category="done")) == "done"
    assert bucket_of(issue("B", category="indeterminate")) == "in_progress"
    assert bucket_of(issue("C", category="new")) == "todo"
    assert bucket_of(issue("D", category="indeterminate", flagged=True)) == "blocked"


# ---------------------------------------------------------------------------
# compute_sprint_metrics
# ---------------------------------------------------------------------------
def sprint_payload(issues, truncated=False):
    return {
        "sprint": {"id": 1234, "name": "Supply Chain Sprint 31", "state": "active"},
        "issues": issues,
        "truncated": truncated,
    }


def test_metrics_bucket_points_sum_to_total():
    metrics = compute_sprint_metrics(
        sprint_payload(
            [
                issue("CSCI-1", category="done", points=5),
                issue("CSCI-2", category="indeterminate", points=3),
                issue("CSCI-3", category="new", points=2),
                issue("CSCI-4", category="indeterminate", points=8, flagged=True),
            ]
        )
    )
    assert metrics["total_points"] == 18
    assert (
        metrics["done_points"]
        + metrics["in_progress_points"]
        + metrics["blocked_points"]
        + metrics["todo_points"]
        == metrics["total_points"]
    )
    assert metrics["done_points"] == 5
    assert metrics["blocked_points"] == 8
    assert metrics["in_progress_points"] == 3


def test_metrics_completion_and_risk():
    metrics = compute_sprint_metrics(
        sprint_payload(
            [
                issue("CSCI-1", category="done", points=8),
                issue("CSCI-2", category="new", points=2),
            ]
        )
    )
    assert metrics["completion_rate"] == 0.8
    assert metrics["completion_pct"] == 80.0
    assert metrics["risk_level"] == "LOW"


def test_metrics_high_risk_when_little_is_done():
    metrics = compute_sprint_metrics(
        sprint_payload([issue("CSCI-1", category="new", points=10)])
    )
    assert metrics["risk_level"] == "HIGH"


def test_metrics_empty_sprint_does_not_divide_by_zero():
    metrics = compute_sprint_metrics(sprint_payload([]))
    assert metrics["completion_rate"] == 0.0
    assert metrics["total_points"] == 0


def test_metrics_carries_sprint_identity_and_truncation():
    metrics = compute_sprint_metrics(sprint_payload([], truncated=True))
    assert metrics["sprint_id"] == 1234
    assert metrics["sprint_name"] == "Supply Chain Sprint 31"
    assert metrics["truncated"] is True


def test_truncation_warning_reaches_the_rendered_report():
    metrics = compute_sprint_metrics(sprint_payload([], truncated=True))
    assert "UNDERCOUNT" in render_sprint_report(metrics, [])


# ---------------------------------------------------------------------------
# _normalise_issue
# ---------------------------------------------------------------------------
def test_normalise_extracts_status_category():
    result = JiraClient._normalise_issue(raw_issue("CSCI-1", "Released", "done", 5))
    assert result["status"] == "Released"
    assert result["status_category"] == "done"
    assert result["story_points"] == 5.0


def test_normalise_reads_the_flagged_field():
    assert JiraClient._normalise_issue(raw_issue("CSCI-1", "To Do", "new", flagged=True))["flagged"]
    assert not JiraClient._normalise_issue(raw_issue("CSCI-2", "To Do", "new"))["flagged"]


def test_normalise_survives_missing_optional_fields():
    result = JiraClient._normalise_issue({"key": "CSCI-9", "fields": {}})
    assert result["key"] == "CSCI-9"
    assert result["story_points"] is None
    assert result["status_category"] == ""


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------
def test_search_pages_until_the_result_set_ends():
    client = make_client(
        [
            {"issues": [raw_issue("CSCI-1", "Done", "done")], "nextPageToken": "t1"},
            {"issues": [raw_issue("CSCI-2", "Done", "done")], "isLast": True},
        ]
    )
    issues, truncated = client.search_issues_page("project = CSCI", max_results=100)

    assert [i["key"] for i in issues] == ["CSCI-1", "CSCI-2"]
    assert truncated is False
    assert client._session.posts[1][1]["nextPageToken"] == "t1"


def test_search_reports_truncation_when_the_cap_is_hit():
    client = make_client(
        [{"issues": [raw_issue("CSCI-1", "Done", "done")], "nextPageToken": "t1"}]
    )
    issues, truncated = client.search_issues_page("project = CSCI", max_results=1)

    assert len(issues) == 1
    assert truncated is True


def test_search_never_requests_more_than_the_page_ceiling():
    client = make_client([{"issues": [], "isLast": True}])
    client.search_issues_page("project = CSCI", max_results=5000)
    assert client._session.posts[0][1]["maxResults"] == 100


def test_search_adds_a_default_order_by():
    client = make_client([{"issues": [], "isLast": True}])
    client.search_issues_page("project = CSCI")
    assert "ORDER BY created DESC" in client._session.posts[0][1]["jql"]


def test_search_respects_a_caller_supplied_order_by():
    client = make_client([{"issues": [], "isLast": True}])
    client.search_issues_page("project = CSCI ORDER BY updated ASC")
    assert client._session.posts[0][1]["jql"].count("ORDER BY") == 1


# ---------------------------------------------------------------------------
# Sprint resolution
# ---------------------------------------------------------------------------
def sprint_lookup_client(sprint_names):
    return make_client(
        [
            {"values": [{"id": 1, "name": "CSCI board", "type": "scrum"}]},
            {
                "values": [
                    {"id": 1000 + n, "name": name, "state": "closed"}
                    for n, name in enumerate(sprint_names)
                ],
                "isLast": True,
            },
        ]
    )


def test_visible_number_resolves_via_the_name_template():
    client = sprint_lookup_client(["Supply Chain Sprint 30", "Supply Chain Sprint 31"])
    assert client.resolve_sprint("CSCI", 31) == 1001


def test_full_sprint_name_also_resolves():
    client = sprint_lookup_client(["Supply Chain Sprint 31"])
    assert client.resolve_sprint("CSCI", "Supply Chain Sprint 31") == 1000


def test_unknown_sprint_lists_what_is_available():
    client = sprint_lookup_client(["Supply Chain Sprint 30"])
    with pytest.raises(ValueError, match="Supply Chain Sprint 30"):
        client.resolve_sprint("CSCI", 99)


def test_active_sprint_wins_when_names_collide_across_boards():
    client = make_client(
        [
            {
                "values": [
                    {"id": 1, "name": "board A", "type": "scrum"},
                    {"id": 2, "name": "board B", "type": "scrum"},
                ]
            },
            {"values": [{"id": 500, "name": "Supply Chain Sprint 31", "state": "closed"}], "isLast": True},
            {"values": [{"id": 900, "name": "Supply Chain Sprint 31", "state": "active"}], "isLast": True},
        ]
    )
    assert client.resolve_sprint("CSCI", 31) == 900


def test_project_sprints_are_cached_after_the_first_walk():
    client = sprint_lookup_client(["Supply Chain Sprint 31"])
    client.resolve_sprint("CSCI", 31)
    calls_after_first = len(client._session.gets)

    # A second resolve must not re-walk the boards — and would raise IndexError
    # on the exhausted payload queue if it tried.
    client.resolve_sprint("CSCI", 31)
    assert len(client._session.gets) == calls_after_first


# ---------------------------------------------------------------------------
# Sprint issues go through JQL, not the Agile issue endpoint
# ---------------------------------------------------------------------------
def test_sprint_issues_use_jql_with_the_internal_id():
    client = make_client(
        [
            {"id": 1234, "name": "Supply Chain Sprint 31", "state": "active"},
            {"issues": [raw_issue("CSCI-1", "Done", "done", 5)], "isLast": True},
        ]
    )
    payload = client.get_sprint_issues(1234)

    assert payload["sprint"]["name"] == "Supply Chain Sprint 31"
    assert payload["issues"][0]["key"] == "CSCI-1"
    assert payload["truncated"] is False

    # Metadata via the Agile API, issues via JQL search.
    assert client._session.gets[0][0].endswith("/rest/agile/1.0/sprint/1234")
    assert client._session.posts[0][0].endswith("/rest/api/3/search/jql")
    assert "sprint = 1234" in client._session.posts[0][1]["jql"]


# ---------------------------------------------------------------------------
# build_issue_fields — the draft -> Jira payload mapping
# ---------------------------------------------------------------------------
def minimal_draft(**overrides):
    return {"summary": "Do the thing", "description": "Because.", **overrides}


def test_acceptance_criteria_actually_reach_the_payload():
    # The old code dropped these entirely: the agent drafted them and they
    # never arrived in Jira.
    fields = build_issue_fields(
        minimal_draft(acceptance_criteria=["Given A, then B"])
    )
    rendered = str(fields["description"])
    assert "Acceptance Criteria" in rendered
    assert "Given A, then B" in rendered


def test_description_is_adf_not_a_bare_string():
    fields = build_issue_fields(minimal_draft())
    assert fields["description"]["type"] == "doc"
    assert fields["description"]["version"] == 1


def test_optional_fields_are_omitted_rather_than_sent_as_null():
    fields = build_issue_fields(minimal_draft())
    for key in ("priority", "parent", "assignee", "components", "labels"):
        assert key not in fields


def test_optional_fields_map_to_jira_shapes():
    fields = build_issue_fields(
        minimal_draft(
            priority="High",
            parent_key="CSCI-100",
            assignee_account_id="acc-1",
            components=["FX Pipeline"],
            labels=["fx"],
            story_points=5,
        )
    )
    assert fields["priority"] == {"name": "High"}
    assert fields["parent"] == {"key": "CSCI-100"}
    assert fields["assignee"] == {"id": "acc-1"}
    assert fields["components"] == [{"name": "FX Pipeline"}]
    assert fields["labels"] == ["fx"]
    assert fields["customfield_10052"] == 5


def test_project_key_defaults_but_can_be_overridden_per_draft():
    assert build_issue_fields(minimal_draft())["project"] == {"key": "CSCI"}
    assert build_issue_fields(minimal_draft(project_key="OTHER"))["project"] == {"key": "OTHER"}


def test_zero_story_points_is_sent_not_treated_as_absent():
    assert build_issue_fields(minimal_draft(story_points=0))["customfield_10052"] == 0


# ---------------------------------------------------------------------------
# Draft parsing and the ticket standard
# ---------------------------------------------------------------------------
def test_parse_reports_bad_drafts_by_position_without_losing_good_ones():
    parsed, errors = parse_drafts(
        [minimal_draft(), {"description": "no summary"}, minimal_draft()]
    )
    assert len(parsed) == 2
    assert len(errors) == 1
    assert "Draft 2" in errors[0]


def test_unknown_issue_type_is_rejected_at_parse_time():
    _, errors = parse_drafts([minimal_draft(issue_type="Nonsense")])
    assert errors


def test_story_without_acceptance_criteria_fails_the_standard():
    problems = check_draft_standard(TicketDraft(**minimal_draft(issue_type="Story")))
    assert any("acceptance criteria" in p for p in problems)


def test_task_without_acceptance_criteria_is_fine():
    assert check_draft_standard(TicketDraft(**minimal_draft(issue_type="Task"))) == []


def test_subtask_requires_a_parent():
    problems = check_draft_standard(TicketDraft(**minimal_draft(issue_type="Sub-task")))
    assert any("parent_key" in p for p in problems)

    ok = check_draft_standard(
        TicketDraft(**minimal_draft(issue_type="Sub-task", parent_key="CSCI-1"))
    )
    assert ok == []


def test_non_fibonacci_estimate_is_flagged():
    problems = check_draft_standard(TicketDraft(**minimal_draft(story_points=4)))
    assert any("Fibonacci" in p for p in problems)


def test_thirteen_points_is_flagged_as_a_smell():
    problems = check_draft_standard(TicketDraft(**minimal_draft(story_points=13)))
    assert any("splitting" in p for p in problems)


def test_overlong_summary_is_flagged():
    problems = check_draft_standard(TicketDraft(**minimal_draft(summary="x" * 81)))
    assert any("max 80" in p for p in problems)


def test_missing_estimate_is_allowed():
    assert check_draft_standard(TicketDraft(**minimal_draft(story_points=None))) == []


# ---------------------------------------------------------------------------
# Bulk creation
# ---------------------------------------------------------------------------
def test_bulk_create_returns_one_result_per_draft():
    client = make_client([{"issues": [{"key": "CSCI-1"}, {"key": "CSCI-2"}], "errors": []}])
    results = client.create_issues([minimal_draft(summary="a"), minimal_draft(summary="b")])

    assert [r["key"] for r in results] == ["CSCI-1", "CSCI-2"]
    assert client._session.posts[0][0].endswith("/rest/api/3/issue/bulk")


def test_partial_failure_maps_errors_back_to_the_right_draft():
    client = make_client(
        [
            {
                "issues": [{"key": "CSCI-1"}, {"key": "CSCI-3"}],
                "errors": [
                    {
                        "failedElementNumber": 1,
                        "elementErrors": {"errors": {"summary": "must not be empty"}},
                    }
                ],
            }
        ]
    )
    results = client.create_issues(
        [minimal_draft(summary="a"), minimal_draft(summary="b"), minimal_draft(summary="c")]
    )

    assert len(results) == 3
    assert results[0]["key"] == "CSCI-1"
    # The failure belongs to draft 2 — not to draft 3, whose key must not shift up.
    assert "error" in results[1]
    assert results[1]["summary"] == "b"
    assert "must not be empty" in results[1]["error"]
    assert results[2]["key"] == "CSCI-3"


def test_batches_over_fifty_are_chunked():
    client = make_client(
        [
            {"issues": [{"key": f"CSCI-{i}"} for i in range(50)], "errors": []},
            {"issues": [{"key": "CSCI-50"}], "errors": []},
        ]
    )
    results = client.create_issues([minimal_draft(summary=str(i)) for i in range(51)])

    assert len(results) == 51
    assert len(client._session.posts) == 2
    assert len(client._session.posts[0][1]["issueUpdates"]) == 50
    assert len(client._session.posts[1][1]["issueUpdates"]) == 1


def test_missing_jira_result_is_reported_not_silently_dropped():
    client = make_client([{"issues": [], "errors": []}])
    results = client.create_issues([minimal_draft(summary="a")])

    assert len(results) == 1
    assert "error" in results[0]


def test_all_drafts_rejected_still_names_the_field_jira_refused():
    """Jira answers 400 when every issue in the chunk fails.

    The body still carries `errors[]`, so the rejection must be read out of it.
    Raising on the status instead left the user with "400 Client Error: Bad
    Request" and no way to learn which field was at fault.
    """
    client = make_client(
        [
            FakeResponse(
                {
                    "issues": [],
                    "errors": [
                        {
                            "failedElementNumber": 0,
                            "elementErrors": {
                                "errors": {
                                    "customfield_10020": "Field 'customfield_10020' "
                                    "cannot be set. It is not on the appropriate "
                                    "screen, or unknown."
                                }
                            },
                        }
                    ],
                },
                status_code=400,
            )
        ]
    )
    results = client.create_issues([minimal_draft(summary="a")])

    assert len(results) == 1
    assert "customfield_10020" in results[0]["error"]
    assert "not on the appropriate screen" in results[0]["error"]


def test_bulk_create_raises_with_detail_when_there_is_no_error_array():
    """A 400 with no `errors[]` is a request-level rejection, not a per-issue one.

    There is nothing to map back to a draft, so it escalates — but it must still
    carry Jira's explanation rather than the bare status.
    """
    client = make_client(
        [FakeResponse({"errorMessages": ["Issue type is required."]}, status_code=400)]
    )

    with pytest.raises(ValueError, match="Issue type is required"):
        client.create_issues([minimal_draft(summary="a")])


# ---------------------------------------------------------------------------
# Assignee resolution — must never guess between people
# ---------------------------------------------------------------------------
def user(name, account_id, email=None, active=True):
    return {
        "display_name": name,
        "account_id": account_id,
        "email": email,
        "active": active,
    }


def test_single_candidate_resolves():
    match, alts = pick_assignee([user("Han Li", "acc-1")], "Han Li")
    assert match["account_id"] == "acc-1"
    assert alts == []


def test_exact_name_wins_over_partial_matches():
    match, _ = pick_assignee(
        [user("Han Li", "acc-1"), user("Hannah Lithgow", "acc-2")], "Han Li"
    )
    assert match["account_id"] == "acc-1"


def test_exact_email_resolves_unambiguously():
    match, _ = pick_assignee(
        [
            user("Han Li", "acc-1", "han.li@example.com"),
            user("Han Li", "acc-2", "h.li@example.com"),
        ],
        "h.li@example.com",
    )
    assert match["account_id"] == "acc-2"


def test_two_people_with_the_same_name_refuses_to_choose():
    match, alts = pick_assignee(
        [user("Han Li", "acc-1"), user("Han Li", "acc-2")], "Han Li"
    )
    assert match is None
    assert len(alts) == 2


def test_ambiguous_partial_match_refuses_to_choose():
    match, alts = pick_assignee(
        [user("Han Li", "acc-1"), user("Hannah Lithgow", "acc-2")], "Ha"
    )
    assert match is None
    assert len(alts) == 2


def test_inactive_accounts_are_never_matched():
    match, _ = pick_assignee(
        [user("Han Li", "old", active=False), user("Han Li", "current")], "Han Li"
    )
    assert match["account_id"] == "current"


def test_only_inactive_candidates_means_no_match():
    match, alts = pick_assignee([user("Han Li", "old", active=False)], "Han Li")
    assert match is None
    assert alts == []


def test_no_candidates_means_no_match():
    assert pick_assignee([], "Nobody") == (None, [])


def test_candidate_list_shows_what_the_user_needs_to_disambiguate():
    rendered = format_candidates([user("Han Li", "acc-1", "han.li@example.com")])
    assert "Han Li" in rendered
    assert "han.li@example.com" in rendered
    assert "acc-1" in rendered


# ---------------------------------------------------------------------------
# Assigning an existing issue
# ---------------------------------------------------------------------------
def test_assign_issue_puts_the_account_id():
    client = make_client([{}])
    client.assign_issue("CSCI-1841", "acc-1")

    url, body = client._session.puts[0]
    assert url.endswith("/rest/api/3/issue/CSCI-1841/assignee")
    assert body == {"accountId": "acc-1"}


def test_unassign_sends_a_null_account_id():
    client = make_client([{}])
    client.assign_issue("CSCI-1841", None)
    assert client._session.puts[0][1] == {"accountId": None}


def test_assignable_search_is_scoped_to_the_project():
    client = make_client([[{"accountId": "acc-1", "displayName": "Han Li", "active": True}]])
    users = client.search_assignable_users("CSCI", "Han Li")

    assert users[0]["account_id"] == "acc-1"
    assert client._session.gets[0][1]["project"] == "CSCI"


def test_users_without_an_account_id_are_dropped():
    client = make_client([[{"displayName": "Ghost"}]])
    assert client.search_assignable_users("CSCI", "Ghost") == []


# ---------------------------------------------------------------------------
# Assignment via the create path
# ---------------------------------------------------------------------------
def test_named_assignee_maps_to_the_jira_assignee_field():
    fields = build_issue_fields(minimal_draft(assignee_account_id="acc-1"))
    assert fields["assignee"] == {"id": "acc-1"}


def test_unresolved_name_alone_is_not_sent_to_jira():
    # `assignee` is a name for resolution, not something Jira accepts directly.
    fields = build_issue_fields(minimal_draft(assignee="Han Li"))
    assert "assignee" not in fields


# ---------------------------------------------------------------------------
# days_in_status — needs an expanded changelog to mean anything
# ---------------------------------------------------------------------------
from datetime import UTC, datetime  # noqa: E402

from pmagent.tools.jira import days_in_current_status  # noqa: E402

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def changelog_issue(histories, created="2026-07-01T09:00:00.000+0000"):
    return {
        "key": "CSCI-1",
        "fields": {"created": created, "status": {"name": "In Progress"}},
        "changelog": {"histories": histories},
    }


def test_days_in_status_uses_the_latest_status_change():
    raw = changelog_issue([
        {"created": "2026-07-10T09:00:00.000+0000", "items": [{"field": "status"}]},
        {"created": "2026-07-28T09:00:00.000+0000", "items": [{"field": "status"}]},
    ])
    assert days_in_current_status(raw, now=NOW) == 5


def test_non_status_changes_are_ignored():
    raw = changelog_issue([
        {"created": "2026-07-10T09:00:00.000+0000", "items": [{"field": "status"}]},
        {"created": "2026-08-01T09:00:00.000+0000", "items": [{"field": "assignee"}]},
    ])
    assert days_in_current_status(raw, now=NOW) == 23


def test_never_transitioned_falls_back_to_created():
    assert days_in_current_status(changelog_issue([]), now=NOW) == 32


def test_no_changelog_requested_reports_zero_rather_than_guessing():
    raw = {"key": "CSCI-1", "fields": {"created": "2026-07-01T09:00:00.000+0000"}}
    assert days_in_current_status(raw, now=NOW) == 0


def test_unparseable_timestamp_does_not_crash():
    raw = changelog_issue([{"created": "not-a-date", "items": [{"field": "status"}]}])
    assert days_in_current_status(raw, now=NOW) == 32


def test_future_timestamp_clamps_to_zero():
    raw = changelog_issue([
        {"created": "2026-08-10T09:00:00.000+0000", "items": [{"field": "status"}]},
    ])
    assert days_in_current_status(raw, now=NOW) == 0


def test_stuck_detection_now_actually_fires():
    # This branch of compute_sprint_metrics was dead while days_in_status was
    # hardcoded to 0. In-flight work stalled past the threshold is blocked.
    stale = issue("CSCI-9", category="indeterminate", points=3, days_in_status=30)
    assert bucket_of(stale) == "blocked"
    assert compute_sprint_metrics(sprint_payload([stale]))["blocked_points"] == 3


def test_recently_touched_work_is_not_stuck():
    fresh = issue("CSCI-10", category="indeterminate", points=3, days_in_status=1)
    assert bucket_of(fresh) == "in_progress"


def test_sprint_search_requests_the_changelog():
    client = make_client([
        {"id": 1234, "name": "Supply Chain Sprint 31", "state": "active"},
        {"issues": [], "isLast": True},
    ])
    client.get_sprint_issues(1234)
    assert client._session.posts[0][1]["expand"] == "changelog"


def test_plain_search_does_not_request_the_changelog():
    client = make_client([{"issues": [], "isLast": True}])
    client.search_issues_page("project = CSCI")
    assert "expand" not in client._session.posts[0][1]


def test_todo_work_is_never_stuck_no_matter_how_old():
    # Backlog age is not a blocker. This is what stopped 105/129 tickets in a
    # real sprint from being reported as blocked.
    ancient = issue("CSCI-11", category="new", points=3, days_in_status=317)
    assert not is_blocked(ancient)
    assert bucket_of(ancient) == "todo"


def test_in_flight_work_past_the_threshold_is_stuck():
    assert is_blocked(issue("CSCI-12", category="indeterminate", days_in_status=30))


def test_in_flight_work_inside_the_threshold_is_not_stuck():
    assert not is_blocked(issue("CSCI-13", category="indeterminate", days_in_status=2))


def test_threshold_is_overridable():
    stalled = issue("CSCI-14", category="indeterminate", days_in_status=5)
    assert is_blocked(stalled, stuck_threshold_days=3)
    assert not is_blocked(stalled, stuck_threshold_days=30)


def test_explicit_flag_beats_the_status_category():
    # A flagged To Do ticket is still blocked — someone said so explicitly.
    assert is_blocked(issue("CSCI-15", category="new", days_in_status=0, flagged=True))


# ---------------------------------------------------------------------------
# Editing an existing issue
# ---------------------------------------------------------------------------
def test_update_only_sends_the_fields_supplied():
    fields = build_issue_update_fields({"story_points": 5})
    assert fields == {"customfield_10052": 5}


def test_update_maps_fields_the_same_way_creation_does():
    fields = build_issue_update_fields(
        {
            "summary": "New summary",
            "issue_type": "Bug",
            "labels": ["fx"],
            "components": ["FX Pipeline"],
            "priority": "High",
            "parent_key": "CSCI-100",
        }
    )
    assert fields["summary"] == "New summary"
    assert fields["issuetype"] == {"name": "Bug"}
    assert fields["labels"] == ["fx"]
    assert fields["components"] == [{"name": "FX Pipeline"}]
    assert fields["priority"] == {"name": "High"}
    assert fields["parent"] == {"key": "CSCI-100"}


def test_update_renders_acceptance_criteria_into_the_description():
    fields = build_issue_update_fields(
        {"description": "Body", "acceptance_criteria": ["Given A, then B"]}
    )
    rendered = str(fields["description"])
    assert "Acceptance Criteria" in rendered
    assert "Given A, then B" in rendered


def test_criteria_without_a_description_is_refused():
    # Sending criteria alone would replace the whole description with just them.
    with pytest.raises(ValueError, match="description"):
        build_issue_update_fields({"acceptance_criteria": ["Given A, then B"]})


def test_sprint_cannot_be_changed_through_an_edit():
    # Jira rejects the sprint field on the edit screen; fail loudly here rather
    # than let the caller think the move happened.
    with pytest.raises(ValueError, match="move_jira_issues_to_sprint"):
        build_issue_update_fields({"sprint_id": 1234})


def test_update_issue_puts_the_mapped_fields():
    client = make_client([{}])
    client.update_issue("CSCI-1709", {"story_points": 5})

    url, body = client._session.puts[0]
    assert url.endswith("/rest/api/3/issue/CSCI-1709")
    assert body == {"fields": {"customfield_10052": 5}}


def test_update_issue_with_nothing_to_change_is_an_error():
    client = make_client([{}])
    with pytest.raises(ValueError, match="No updatable fields"):
        client.update_issue("CSCI-1709", {})
    assert client._session.puts == []


# ---------------------------------------------------------------------------
# Moving existing issues between sprints
# ---------------------------------------------------------------------------
def test_move_posts_the_keys_to_the_agile_sprint_endpoint():
    client = make_client([{}])
    results = client.move_issues_to_sprint(1234, ["CSCI-1709", "CSCI-1710"])

    url, body = client._session.posts[0]
    assert url.endswith("/rest/agile/1.0/sprint/1234/issue")
    assert body == {"issues": ["CSCI-1709", "CSCI-1710"]}
    assert [r["error"] for r in results] == [None, None]


def test_move_chunks_at_fifty():
    keys = [f"CSCI-{i}" for i in range(120)]
    client = make_client([{}, {}, {}])
    results = client.move_issues_to_sprint(1234, keys)

    assert [len(body["issues"]) for _, body in client._session.posts] == [50, 50, 20]
    assert [r["key"] for r in results] == keys


def test_a_rejected_chunk_is_reported_against_every_key_in_it():
    # The endpoint fails the whole request, not individual issues — reporting
    # any of them as moved would be a lie.
    client = make_client(
        [FakeResponse({"errorMessages": ["Sprint is closed."]}, status_code=400)]
    )
    results = client.move_issues_to_sprint(1234, ["CSCI-1709", "CSCI-1710"])

    assert [r["error"] for r in results] == ["Sprint is closed.", "Sprint is closed."]


def test_a_failing_chunk_does_not_hide_a_succeeding_one():
    keys = [f"CSCI-{i}" for i in range(60)]
    client = make_client([FakeResponse({}, status_code=400), {}])
    results = client.move_issues_to_sprint(1234, keys)

    assert all(r["error"] for r in results[:50])
    assert not any(r["error"] for r in results[50:])


def test_field_level_errors_are_surfaced():
    client = make_client(
        [FakeResponse({"errors": {"issues": "Issue does not exist."}}, status_code=400)]
    )
    results = client.move_issues_to_backlog(["CSCI-9999"])
    assert results[0]["error"] == "issues: Issue does not exist."


def test_removing_from_a_sprint_posts_to_the_backlog_endpoint():
    client = make_client([{}])
    client.move_issues_to_backlog(["CSCI-1709"])

    url, body = client._session.posts[0]
    assert url.endswith("/rest/agile/1.0/backlog/issue")
    assert body == {"issues": ["CSCI-1709"]}


# ---------------------------------------------------------------------------
# Commenting on existing issues
# ---------------------------------------------------------------------------
def test_comment_posts_an_adf_body_to_each_issue():
    client = make_client([{}, {}])
    results = client.add_comments(["CSCI-1709", "CSCI-1710"], "Close if no further issue.")

    urls = [url for url, _ in client._session.posts]
    assert urls[0].endswith("/rest/api/3/issue/CSCI-1709/comment")
    assert urls[1].endswith("/rest/api/3/issue/CSCI-1710/comment")
    assert [r["error"] for r in results] == [None, None]

    # ADF, not a plain string — Jira rejects a string body here like it does on
    # a description.
    body = client._session.posts[0][1]["body"]
    assert body["type"] == "doc"
    assert body["content"][0]["content"][0]["text"] == "Close if no further issue."


def test_comment_markdown_is_rendered():
    client = make_client([{}])
    client.add_comments(["CSCI-1709"], "## Status\n\n- **blocked** on `dbt run`")

    content = client._session.posts[0][1]["body"]["content"]
    assert content[0]["type"] == "heading"
    assert content[1]["type"] == "bulletList"


def test_one_failed_comment_does_not_condemn_the_others():
    # No bulk endpoint means one request per issue, so unlike a sprint move a
    # failure really is per-issue and must not be reported against the batch.
    client = make_client(
        [
            {},
            FakeResponse({"errorMessages": ["Issue does not exist."]}, status_code=404),
            {},
        ]
    )
    results = client.add_comments(["CSCI-1709", "CSCI-9999", "CSCI-1710"], "Ping.")

    assert [r["key"] for r in results] == ["CSCI-1709", "CSCI-9999", "CSCI-1710"]
    assert [r["error"] for r in results] == [None, "Issue does not exist.", None]


# ---------------------------------------------------------------------------
# Owner visibility — fetched is not the same as shown
# ---------------------------------------------------------------------------
def test_the_owner_appears_in_a_search_line():
    # The data was always fetched and normalised; it just never reached the
    # agent, which then reported ownership as unavailable.
    line = format_issue_line(issue("CSCI-1", points=2, assignee="Alan Yuen"))
    assert "Alan Yuen" in line


def test_an_unowned_ticket_says_so_rather_than_leaving_a_gap():
    line = format_issue_line(issue("CSCI-1", points=2))
    assert "unassigned" in line


def test_blocked_tickets_carry_their_owner_into_the_sprint_report():
    metrics = compute_sprint_metrics({
        "sprint": {"name": "S31"},
        "issues": [issue("CSCI-1", status="Blocked", category="indeterminate",
                         points=2, assignee="Alan Yuen")],
    })
    assert metrics["blocked_tickets"][0]["assignee"] == "Alan Yuen"
    assert "Alan Yuen" in render_sprint_report(metrics, [])


# ---------------------------------------------------------------------------
# Deep reads: description + comments
# ---------------------------------------------------------------------------
def adf_doc(text):
    return {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": text}]}
    ]}


def test_issue_detail_renders_description_and_comments_as_text():
    raw = raw_issue("CSCI-1690", "Blocked", "indeterminate", points=2)
    raw["fields"]["description"] = adf_doc("Waiting on the SCAX extract.")
    client = make_client([
        raw,
        {"total": 1, "comments": [
            {"author": {"displayName": "Alan Yuen"},
             "created": "2026-07-30T09:00:00.000+1000",
             "body": adf_doc("Chased the source team.")},
        ]},
    ])

    detail = client.get_issue_detail("CSCI-1690")
    assert detail["description"] == "Waiting on the SCAX extract."
    assert detail["comments"][0]["author"] == "Alan Yuen"
    assert detail["comments"][0]["body"] == "Chased the source team."
    assert detail["assignee"] == "Han Li"


def test_only_the_most_recent_comments_are_kept():
    # Newest last in Jira's ordering, so the tail is the live conversation.
    raw = raw_issue("CSCI-1690", "Blocked", "indeterminate")
    raw["fields"]["description"] = adf_doc("x")
    comments = [
        {"author": {"displayName": "A"}, "created": "2026-07-0%dT09:00:00.000+1000" % i,
         "body": adf_doc(f"comment {i}")}
        for i in range(1, 6)
    ]
    client = make_client([raw, {"total": 5, "comments": comments}])

    detail = client.get_issue_detail("CSCI-1690", max_comments=2)
    assert [c["body"] for c in detail["comments"]] == ["comment 4", "comment 5"]
    # The count of what exists, not of what was shown.
    assert detail["comments"][0]["total"] == 5


def test_the_detail_request_asks_for_the_description_field():
    raw = raw_issue("CSCI-1690", "Blocked", "indeterminate")
    client = make_client([raw, {"total": 0, "comments": []}])
    client.get_issue_detail("CSCI-1690")

    assert "description" in client._session.gets[0][1]["fields"]


def test_a_missing_description_renders_empty_not_crashing():
    raw = raw_issue("CSCI-1690", "Blocked", "indeterminate")
    raw["fields"]["description"] = None
    client = make_client([raw, {"total": 0, "comments": []}])

    detail = client.get_issue_detail("CSCI-1690")
    assert detail["description"] == ""
    assert "(empty)" in render_issue_detail(detail)


def test_rendered_detail_shows_how_many_comments_were_withheld():
    detail = {
        "key": "CSCI-1690", "type": "Task", "status": "Blocked", "summary": "x",
        "assignee": None, "story_points": 2, "description": "why",
        "comments": [{"author": "A", "created": "2026-07-30", "body": "b",
                      "total": 22, "shown": 1}],
    }
    out = render_issue_detail(detail)
    assert "showing 1 of 22" in out
    assert "unassigned" in out


def test_long_bodies_are_truncated_out_loud():
    out = _clip("x" * 2000)
    assert out.endswith("more characters]")
    assert "truncated" in out


# ---------------------------------------------------------------------------
# Shared batch reporting
# ---------------------------------------------------------------------------
def test_a_partly_failed_batch_never_reads_as_done():
    # One renderer behind every batch write, so no tool can quietly round a
    # partial failure up to success.
    out = _render_batch_result(
        [{"key": "CSCI-1", "error": None}, {"key": "CSCI-2", "error": "Sprint is closed."}],
        "moved",
        "the backlog",
    )
    assert out.startswith("Moved 1 of 2 issue(s) to the backlog.")
    assert "FAILED (1) — these were NOT moved:" in out
    assert "- CSCI-2: Sprint is closed." in out


def test_the_verb_drives_both_the_headline_and_the_failure_note():
    out = _render_batch_result([{"key": "CSCI-1", "error": "Boom."}], "commented on")
    assert out.startswith("Commented on 0 of 1 issue(s).")
    assert "these were NOT commented on:" in out


def test_success_lines_can_carry_more_than_the_key():
    out = _render_batch_result(
        [{"key": "CSCI-1", "error": None, "to_status": "In review"}],
        "moved",
        "'In Review'",
        describe=lambda r: f"{r['key']} -> {r['to_status']}",
    )
    # The status Jira actually used, not the one the user typed.
    assert "- CSCI-1 -> In review" in out


# ---------------------------------------------------------------------------
# Workflow transitions
# ---------------------------------------------------------------------------
TRANSITIONS = [
    {"id": "21", "name": "Start Progress", "to_status": "In Progress"},
    {"id": "31", "name": "Ready for Review", "to_status": "In Review"},
    {"id": "41", "name": "Done", "to_status": "Done"},
]


def test_transition_is_matched_on_the_destination_status():
    # Users name where the ticket should end up, not what the arrow is called.
    match, ambiguous = pick_transition(TRANSITIONS, "In Review")
    assert match["id"] == "31"
    assert ambiguous == []


def test_transition_can_also_be_matched_on_its_own_name():
    match, _ = pick_transition(TRANSITIONS, "Start Progress")
    assert match["id"] == "21"


def test_destination_wins_over_transition_name():
    # "Done" is both a transition name and a destination; the destination
    # reading is what the user meant.
    overlapping = [
        {"id": "51", "name": "Done", "to_status": "Closed"},
        {"id": "41", "name": "Finish", "to_status": "Done"},
    ]
    match, _ = pick_transition(overlapping, "Done")
    assert match["id"] == "41"


def test_transition_matching_ignores_case_and_padding():
    match, _ = pick_transition(TRANSITIONS, "  in review ")
    assert match["id"] == "31"


def test_an_unmatched_status_picks_nothing_rather_than_the_closest():
    # "Review" is not "In Review". Guessing moves real work to a wrong state.
    match, ambiguous = pick_transition(TRANSITIONS, "Review")
    assert match is None
    assert ambiguous == []


def test_several_routes_to_one_status_are_reported_not_guessed():
    duplicated = [
        {"id": "31", "name": "Ready for Review", "to_status": "In Review"},
        {"id": "32", "name": "Send Back for Review", "to_status": "In Review"},
    ]
    match, ambiguous = pick_transition(duplicated, "In Review")
    assert match is None
    assert [t["id"] for t in ambiguous] == ["31", "32"]


def raw_transitions(*items):
    return {"transitions": [
        {"id": i, "name": n, "to": {"name": s}} for i, n, s in items
    ]}


def test_transition_posts_the_looked_up_id():
    client = make_client([raw_transitions(("31", "Ready for Review", "In Review")), {}])
    results = client.transition_issues(["CSCI-1805"], "In Review")

    url, body = client._session.posts[0]
    assert url.endswith("/rest/api/3/issue/CSCI-1805/transitions")
    assert body == {"transition": {"id": "31"}}
    assert results == [{"key": "CSCI-1805", "error": None, "to_status": "In Review"}]


def test_each_issue_gets_its_own_transition_lookup():
    # Two tickets in different statuses need different ids for the same
    # destination — which is why this can't be one batched call.
    client = make_client([
        raw_transitions(("31", "Ready for Review", "In Review")),
        {},
        raw_transitions(("33", "Reopen for Review", "In Review")),
        {},
    ])
    client.transition_issues(["CSCI-1805", "CSCI-1806"], "In Review")

    posted = [body for url, body in client._session.posts if "transition" in body]
    assert posted == [{"transition": {"id": "31"}}, {"transition": {"id": "33"}}]


def test_an_unavailable_status_lists_what_is_available():
    client = make_client([
        raw_transitions(("21", "Start Progress", "In Progress")),
        raw_issue("CSCI-1805", "To Do", "new"),
    ])
    results = client.transition_issues(["CSCI-1805"], "In Review")

    error = results[0]["error"]
    assert "no transition to 'In Review' from 'To Do'" in error
    assert "Start Progress -> In Progress" in error
    assert client._session.posts == []  # nothing was written


def test_an_issue_already_in_the_target_status_says_so():
    # Jira offers no transition to where you already are; "no transition found"
    # would send the user hunting for a workflow problem that isn't there.
    client = make_client([
        raw_transitions(("41", "Done", "Done")),
        raw_issue("CSCI-1805", "In Review", "indeterminate"),
    ])
    results = client.transition_issues(["CSCI-1805"], "In Review")
    assert results[0]["error"] == "already in 'In Review'; nothing to do."


def test_one_unmatched_issue_does_not_stop_the_rest():
    client = make_client([
        raw_transitions(("31", "Ready for Review", "In Review")),
        {},
        raw_transitions(("21", "Start Progress", "In Progress")),
        raw_issue("CSCI-1806", "To Do", "new"),
    ])
    results = client.transition_issues(["CSCI-1805", "CSCI-1806"], "In Review")

    assert results[0]["error"] is None
    assert results[1]["error"]


# ---------------------------------------------------------------------------
# Sprint on create — the visible number is not the internal id
# ---------------------------------------------------------------------------
class _SprintRecordingClient:
    """Records what `create_jira_issues` resolved before it called Jira."""

    def __init__(self, sprint_map):
        self._sprint_map = sprint_map
        self.created = None

    def resolve_sprint(self, project_key, sprint_ref):
        try:
            return self._sprint_map[str(sprint_ref)]
        except KeyError:
            raise ValueError(f"No sprint found with name '{sprint_ref}'.") from None

    def create_issues(self, drafts):
        self.created = drafts
        return [{"summary": d.get("summary", ""), "key": "CSCI-1"} for d in drafts]


def test_draft_sprint_number_is_resolved_to_the_internal_id(monkeypatch):
    """"Sprint 31" is what a person says; Jira's field wants 3435.

    Passing the visible number straight through does not error — it files the
    ticket into a real but different sprint, which nothing downstream would
    ever flag.
    """
    from pmagent.tools.jira import tools_write

    fake = _SprintRecordingClient({"31": 3435})
    monkeypatch.setattr(tools_write, "get_client", lambda: fake)

    out = tools_write.create_jira_issues(**
        {"drafts": [minimal_draft(summary="a", sprint=31)]}
    )

    assert fake.created[0]["sprint_id"] == 3435
    assert "Created 1 of 1" in out


def test_an_unresolvable_sprint_creates_nothing(monkeypatch):
    from pmagent.tools.jira import tools_write

    fake = _SprintRecordingClient({"31": 3435})
    monkeypatch.setattr(tools_write, "get_client", lambda: fake)

    out = tools_write.create_jira_issues(**
        {"drafts": [minimal_draft(summary="a", sprint=99)]}
    )

    assert fake.created is None
    assert "Nothing was created" in out


def test_an_explicit_internal_sprint_id_is_left_alone(monkeypatch):
    from pmagent.tools.jira import tools_write

    fake = _SprintRecordingClient({})
    monkeypatch.setattr(tools_write, "get_client", lambda: fake)

    tools_write.create_jira_issues(**
        {"drafts": [minimal_draft(summary="a", sprint_id=3435)]}
    )

    assert fake.created[0]["sprint_id"] == 3435


def test_build_issue_fields_uses_the_configured_sprint_field():
    fields = build_issue_fields(minimal_draft(summary="a", sprint_id=3435))
    assert fields[get_sprint_field()] == 3435


# ---------------------------------------------------------------------------
# Exact key lookup — the precise counterpart to `text ~` searching
# ---------------------------------------------------------------------------
def test_key_clause_matches_an_exact_set():
    jql = JqlBuilder().keys("CSCI-1712", "CSCI-1714").build()
    assert 'key IN ("CSCI-1712", "CSCI-1714")' in jql


def test_key_clause_ignores_blanks_rather_than_emitting_empty_in():
    # `key IN ()` is a JQL syntax error, and an empty clause list is caught by
    # build() — silently dropping the clause would run an unscoped query.
    with pytest.raises(ValueError):
        JqlBuilder().keys("", "   ").build()


def test_source_issue_reaches_the_jira_description():
    fields = build_issue_fields(
        minimal_draft(summary="a", source_issue="CSCI-1379")
    )
    assert "CSCI-1379" in str(fields["description"])


def test_a_draft_without_a_source_issue_says_nothing_about_one():
    fields = build_issue_fields(minimal_draft(summary="a"))
    assert "Related" not in str(fields["description"])


# ---------------------------------------------------------------------------
# The scope reconciler — intent vs action (docs/harness-handover.md §9)
# ---------------------------------------------------------------------------
def _scoped(summary, source=None):
    return TicketDraft(**minimal_draft(summary=summary, source_issue=source))


def test_matching_sets_reconcile_clean():
    drafts = [_scoped("a", "CSCI-1712"), _scoped("b", "CSCI-1714")]
    assert reconcile_scope(drafts, ["CSCI-1712", "CSCI-1714"]) == []


def test_a_draft_outside_the_named_scope_is_reported():
    """The TUTORIAL §4.8 failure, as an assertion.

    Eight keys named, a ninth draft derived from a card nobody mentioned. Every
    field on that draft is well-formed, which is why nothing else catches it.
    """
    drafts = [_scoped("a", "CSCI-1380"), _scoped("b", "CSCI-1379")]
    problems = reconcile_scope(drafts, ["CSCI-1380"])
    assert len(problems) == 1
    assert "CSCI-1379" in problems[0]
    assert "not in the scope" in problems[0]


def test_a_named_key_with_no_draft_is_reported_too():
    # Legitimate when the user filtered ("only the started ones") — which is
    # exactly why it is reported rather than silently accepted or corrected.
    problems = reconcile_scope([_scoped("a", "CSCI-1712")], ["CSCI-1712", "CSCI-1714"])
    assert any("CSCI-1714" in p and "not drafted" in p for p in problems)


def test_reconciliation_is_case_and_whitespace_insensitive():
    assert reconcile_scope([_scoped("a", " csci-1712 ")], ["CSCI-1712"]) == []


def test_two_drafts_from_one_source_are_flagged_not_blocked_silently():
    drafts = [_scoped("a", "CSCI-1712"), _scoped("b", "CSCI-1712")]
    problems = reconcile_scope(drafts, ["CSCI-1712"])
    assert any("more than one draft" in p for p in problems)


def test_a_draft_with_no_source_cannot_be_checked_and_says_so():
    problems = reconcile_scope([_scoped("a")], ["CSCI-1712"])
    assert any("no source_issue" in p for p in problems)


def test_no_scope_declared_means_no_reconciliation():
    # Backwards compatible: every existing caller passes no scope.
    assert reconcile_scope([_scoped("a", "CSCI-9999")], []) == []


def test_create_aborts_the_whole_batch_on_a_scope_mismatch(monkeypatch):
    """A partly-right batch is worse than none — same rule as the assignee check."""
    from pmagent.tools.jira import tools_write

    fake = _SprintRecordingClient({})
    monkeypatch.setattr(tools_write, "get_client", lambda: fake)

    out = tools_write.create_jira_issues(**{
        "drafts": [
            minimal_draft(summary="a", source_issue="CSCI-1380"),
            minimal_draft(summary="b", source_issue="CSCI-1379"),
        ],
        "scope": ["CSCI-1380"],
    })

    assert fake.created is None
    assert "Nothing was created" in out
    assert "CSCI-1379" in out


def test_create_proceeds_when_the_sets_agree(monkeypatch):
    from pmagent.tools.jira import tools_write

    fake = _SprintRecordingClient({})
    monkeypatch.setattr(tools_write, "get_client", lambda: fake)

    out = tools_write.create_jira_issues(**{
        "drafts": [minimal_draft(summary="a", source_issue="CSCI-1380")],
        "scope": ["CSCI-1380"],
    })

    assert fake.created is not None
    assert "Created 1 of 1" in out
