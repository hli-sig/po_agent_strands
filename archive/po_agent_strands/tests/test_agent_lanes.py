"""Wiring checks for the agent lanes.

These catch the class of bug where a prompt instructs the agent to use a tool
that was never bound to it — which is silent at import time and only shows up
as the agent insisting it can't do the thing you just asked for.
"""

import importlib
import pathlib

import pytest
from strands.tools.decorator import DecoratedFunctionTool

import pmagent.tools
from pmagent import gate
from pmagent.agents import finance_agent, query_agent, sprint_agent, ticket_agent
from pmagent.gate import WRITE_TOOL_NAMES


# Tools that create Jira issues. Only the ticket lane may bind these.
CREATE_TOOLS = {"create_jira_issues", "create_jira_issue"}

# Every lane declared as a (SYSTEM_PROMPT, TOOLS) module. Add new lanes here and
# they inherit the prompt/tool/gate checks below — that is the whole point of
# keeping lanes declarative.
LANES = (sprint_agent, ticket_agent, finance_agent)


def names(tools):
    return {t.tool_name for t in tools}


def defined_tools(module):
    """Every `@tool` *defined* in a module, found by reflection rather than by
    trusting a list — which is the whole point, since the list is what's under
    test. Tools imported from elsewhere are excluded, so a module that binds
    another's tool isn't asked to classify it twice.
    """
    found = set()
    for value in vars(module).values():
        if not isinstance(value, DecoratedFunctionTool):
            continue
        if value._tool_func.__module__ == module.__name__:
            found.add(value.tool_name)
    return found


def discover_tool_modules():
    """Find every module that defines `@tool`s, by walking the source tree.

    Deliberately *not* a hand-written list. A checklist that must be updated
    whenever a tool module is added is the exact failure this whole group of
    tests exists to remove — writing one here would just move the gap up a
    level, where it would be even easier to miss.

    Scope is "the top level of `pmagent/tools/`, plus the `jira` package".
    `fy_budget/` is a directory and so falls outside it, which is right: it is
    Finance's calculation package and defines no tools (and importing it would
    drag in pandas for no reason).
    """
    root = pathlib.Path(pmagent.tools.__file__).parent
    paths = sorted(root.glob("*.py")) + sorted((root / "jira").glob("*.py"))

    modules = []
    for path in paths:
        if path.name == "__init__.py":
            continue
        dotted = "pmagent.tools." + (
            f"jira.{path.stem}" if path.parent.name == "jira" else path.stem
        )
        module = importlib.import_module(dotted)
        if defined_tools(module):
            modules.append(module)
    return tuple(modules)


# Discovered at import, so a new tool module is covered by the gate
# reconciliation below the moment it defines its first `@tool` — no list to
# update, and no way to opt out by forgetting.
TOOL_MODULES = discover_tool_modules()

# The MCP tools the diagram lane binds are NOT here and cannot be: they are
# discovered at runtime from Lucid's server (`tools/mcp_tools.py`). In the
# LangGraph original that was a fail-open gap; here the gate treats any tool it
# does not know as a write (see tests/test_gate.py), so the gap fails closed.


def test_the_gate_reads_the_same_modules_the_tests_discover():
    # gate.READ_TOOL_NAMES is derived from gate.TOOL_MODULES. A tool module the
    # gate doesn't list would have its reads *gated* (safe, but noisy); this
    # keeps the two lists identical so nobody learns to type "y" on reflex.
    assert set(gate.TOOL_MODULES) == set(TOOL_MODULES)


def test_sprint_agent_binds_the_tool_its_prompt_names():
    # The prompt tells the agent to use this for "sprint 31"; it must exist.
    assert "get_sprint_status_by_number" in names(sprint_agent.TOOLS)


def test_every_tool_named_in_a_prompt_is_actually_bound():
    for module in LANES:
        bound = names(module.TOOLS)
        for tool_name in bound:
            assert tool_name in module.SYSTEM_PROMPT, (
                f"{tool_name} is bound but never mentioned in the prompt"
            )


def test_the_query_lane_stays_read_only():
    assert not names(query_agent.TOOLS) & WRITE_TOOL_NAMES


def test_the_query_lane_holds_every_read_only_jira_tool():
    # The mirror of the rule above, and the one that actually bit: "who owns
    # CSCI-1690 and why is it blocked?" routes here, so a lane with only
    # `query_jira_issues` answers half the question and calls the rest
    # unavailable — with no safety reason, since there is nothing to gate.
    read_tools = names(ticket_agent.TOOLS) | names(sprint_agent.TOOLS) - WRITE_TOOL_NAMES
    for tool_name in ("read_jira_issue_details", "list_jira_transitions"):
        assert tool_name in read_tools  # still bound somewhere
        assert tool_name in names(query_agent.TOOLS), (
            f"{tool_name} is read-only but the read-only lane can't use it"
        )


def test_only_the_ticket_lane_creates_issues():
    assert not names(sprint_agent.TOOLS) & CREATE_TOOLS
    assert names(ticket_agent.TOOLS) & CREATE_TOOLS == {"create_jira_issues"}


# Name fragments that suggest a tool mutates something. Crude on purpose.
_MUTATING_PREFIXES = (
    "create_", "update_", "assign_", "move_", "remove_", "add_", "transition_",
    "delete_", "apply_", "propose_", "prepare_", "close_", "archive_", "set_",
)


@pytest.mark.parametrize("module", TOOL_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_no_read_tool_has_a_mutating_name(module):
    # The structural checks below prove every tool is *classified* and that the
    # gate agrees with the classification. Neither can tell you the
    # classification is WRONG — a genuine write filed under READ_TOOLS passes
    # all of them. This heuristic is the only thing looking for that, which is
    # why it survived the refactor that made the rest of it redundant.
    #
    # It is still just a name check, and it only fires on names someone thought
    # of. If it ever objects to a genuinely read-only tool, rename the tool
    # before you touch this list — a read whose name reads as a write will
    # mislead the model too.
    for tool_name in names(module.READ_TOOLS):
        assert not tool_name.startswith(_MUTATING_PREFIXES), (
            f"{tool_name} is declared read-only but its name says otherwise"
        )
        assert tool_name not in WRITE_TOOL_NAMES


def test_discovery_finds_the_tool_modules_we_know_about():
    # Guards the walk itself. If the glob silently stopped matching, every
    # parametrized check below would pass vacuously with an empty module list —
    # a green suite that tests nothing is worse than a red one.
    found = {m.__name__.rsplit(".", 1)[-1] for m in TOOL_MODULES}
    assert {"tools_read", "tools_write", "confluence_tools", "finance_tools",
            "spreadsheet_tools", "diagram_tools"} <= found


@pytest.mark.parametrize("module", TOOL_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_every_tool_module_declares_both_lists(module):
    # An absent list must fail loudly here rather than as an AttributeError
    # halfway through another test. Declaring an empty list is a statement;
    # declaring nothing is an omission, and the two must not look alike.
    for attr in ("READ_TOOLS", "WRITE_TOOLS"):
        assert hasattr(module, attr), (
            f"{module.__name__} defines @tools but no {attr}. Every tool module "
            f"declares both lists, even when one is empty."
        )


@pytest.mark.parametrize("module", TOOL_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_every_write_tool_is_gated(module):
    # The check above is a name heuristic and only sees tools a lane happens to
    # bind; this one is structural. Each tool module *declares* what it mutates,
    # so anything in its WRITE_TOOLS must be in WRITE_TOOL_NAMES — including a
    # tool no lane binds yet, and one whose name the heuristic would miss
    # ("close_jira_issue", "propose_...", "apply_...").
    assert names(module.WRITE_TOOLS) <= WRITE_TOOL_NAMES, (
        f"not gated: {names(module.WRITE_TOOLS) - WRITE_TOOL_NAMES}"
    )


@pytest.mark.parametrize("module", TOOL_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_no_read_tool_is_stuck_behind_the_gate(module):
    # The mirror image, and just as invisible: a read-only tool listed in
    # WRITE_TOOL_NAMES makes the graph stop and ask permission to look
    # something up. Nothing to approve, and the user learns to type "y" on
    # reflex — which is how a real write gets waved through later.
    assert not names(module.READ_TOOLS) & WRITE_TOOL_NAMES


@pytest.mark.parametrize("module", TOOL_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_the_two_lists_do_not_overlap(module):
    # A tool exported from both lists would have an ambiguous gate.
    assert not names(module.READ_TOOLS) & names(module.WRITE_TOOLS)


@pytest.mark.parametrize("module", TOOL_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_every_tool_defined_is_classified(module):
    # The one that actually closes the loop. Declaring the lists is only a
    # guard if forgetting to declare *fails* — otherwise a new tool lands in
    # neither list, matches no name prefix, and is gated by nobody. This asserts
    # the module's own @tools are exactly its READ_TOOLS + WRITE_TOOLS, so the
    # act of adding a tool forces the author to say which side it is on.
    declared = names(module.READ_TOOLS) | names(module.WRITE_TOOLS)
    assert defined_tools(module) == declared, (
        f"unclassified in {module.__name__}: {defined_tools(module) - declared}; "
        f"declared but not defined here: {declared - defined_tools(module)}"
    )


def test_every_gated_name_is_declared_by_some_module():
    # No orphans in WRITE_TOOL_NAMES. A stale or misspelled entry gates nothing,
    # and reads as protection that isn't there — the failure mode this whole
    # group of tests exists to remove.
    declared = {name for m in TOOL_MODULES for name in names(m.WRITE_TOOLS)}
    assert not WRITE_TOOL_NAMES - declared, (
        f"gated but declared by no module: {WRITE_TOOL_NAMES - declared}"
    )


def test_every_gated_tool_has_a_readable_approval_line():
    # The gate only helps if the human can tell what they are approving.
    # `_describe_write` falls back to `name(raw args)`, which is the honest
    # output for a tool nobody described — and a prompt nobody can read is how
    # "y" becomes a reflex. Any tool reaching that fallback is missing a case.
    from pmagent.cli.approval import _describe_write

    for name in sorted(WRITE_TOOL_NAMES):
        described = _describe_write({"name": name, "args": {}})
        assert not described.startswith(f"{name}("), (
            f"{name} has no case in cli/approval.py's _describe_write, so its approval "
            f"prompt shows raw arguments"
        )


def test_existing_tickets_can_be_moved_between_sprints():
    # The agent used to be able to set a sprint only at creation time; both
    # lanes that field scope changes must expose the move.
    for module in (sprint_agent, ticket_agent):
        assert "move_jira_issues_to_sprint" in names(module.TOOLS)
        assert "remove_jira_issues_from_sprint" in names(module.TOOLS)


def test_existing_tickets_can_be_edited():
    assert "update_jira_issue" in names(ticket_agent.TOOLS)


def test_both_lanes_can_move_tickets_through_the_workflow():
    # "Move it to In Review" follows an assignment in the ticket lane and a
    # sprint review in the sprint lane; status is not an editable field, so
    # without this tool neither lane can do it at all.
    for module in (sprint_agent, ticket_agent):
        assert "transition_jira_issues" in names(module.TOOLS)
        assert "list_jira_transitions" in names(module.TOOLS)


def test_both_lanes_can_read_a_ticket_s_content():
    # "Why is this blocked?" needs description and comments; the one-line search
    # result cannot answer it, and the agent correctly refused to invent one.
    for module in (sprint_agent, ticket_agent):
        assert "read_jira_issue_details" in names(module.TOOLS)


def test_reading_ticket_content_is_not_gated():
    assert "read_jira_issue_details" not in WRITE_TOOL_NAMES


def test_reading_transitions_is_not_gated():
    # Looking up where a ticket can go changes nothing; gating it would put an
    # approval prompt in front of a lookup the agent needs to answer a question.
    assert "list_jira_transitions" not in WRITE_TOOL_NAMES


def test_both_lanes_can_comment_on_tickets():
    # "Comment 'close if no further issue' on these" arrives in the sprint lane
    # as often as the ticket one, straight off a sprint review.
    for module in (sprint_agent, ticket_agent):
        assert "add_jira_comment" in names(module.TOOLS)


def test_ticket_lane_can_validate_before_writing():
    # Validation must be available, or the approval step has nothing to show.
    assert "validate_ticket_drafts" in names(ticket_agent.TOOLS)


def test_the_finance_lane_pairs_its_write_with_the_read_that_informs_it():
    # `fy_start` is a value the model cannot know and must not infer, so the
    # look-up ships alongside the conversion — the same pairing as
    # find_jira_user/assign_jira_issue and list_jira_transitions/transition.
    bound = names(finance_agent.TOOLS)
    assert {"inspect_fy_budget_inputs", "create_fy_budget_csv", "read_fy_budget_run"} == bound


def test_generating_the_budget_csv_is_gated():
    assert "create_fy_budget_csv" in WRITE_TOOL_NAMES


def test_reading_fy_budget_state_is_not_gated():
    # Inspecting inputs and reading a run's audit metadata change nothing.
    assert "inspect_fy_budget_inputs" not in WRITE_TOOL_NAMES
    assert "read_fy_budget_run" not in WRITE_TOOL_NAMES


def test_confluence_reads_are_available_wherever_documentation_is_needed():
    # Drafting a ticket against the documented data model needs the page, and
    # "what columns does DimEmployee have?" is a plain look-up that lands in the
    # read-only lane. Neither writes, so neither is gated.
    for tool_name in ("read_confluence_page", "search_confluence"):
        assert tool_name in names(ticket_agent.TOOLS)
        assert tool_name in names(query_agent.TOOLS)
        assert tool_name not in WRITE_TOOL_NAMES


def test_the_query_lane_holds_the_read_only_fy_budget_tools():
    # Gate by consequence, not by lane: "what's in the budget folder?" is a
    # plain look-up and lands here, and there is nothing to gate.
    query = names(query_agent.TOOLS)
    assert {"inspect_fy_budget_inputs", "read_fy_budget_run"} <= query
    assert "create_fy_budget_csv" not in query


def test_finance_prompt_carries_the_partition_rules():
    prompt = finance_agent.SYSTEM_PROMPT
    assert "{skill}" not in prompt
    assert "Banner State" in prompt          # from skills/fy_budget/SKILL.md
    assert "202607" in prompt                # the FY27 fiscal start


def test_finance_prompt_forbids_inventing_figures():
    prompt = finance_agent.SYSTEM_PROMPT.lower()
    assert "never calculate" in prompt
    assert "do not infer" in prompt


def test_ticket_prompt_carries_the_standard():
    assert "{skill}" not in ticket_agent.SYSTEM_PROMPT
    assert "INVEST" in ticket_agent.SYSTEM_PROMPT
    assert "Fibonacci" in ticket_agent.SYSTEM_PROMPT


def test_ticket_prompt_keeps_the_human_approval_gate():
    prompt = ticket_agent.SYSTEM_PROMPT
    assert "confirm" in prompt.lower()
    assert "only after" in prompt.lower()


# ---------------------------------------------------------------------------
# Write gate — see tests/test_gate.py, which drives the real Strands agent loop.
# (The LangGraph original tested `pending_write_calls` on graph state here.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Routing completeness
# ---------------------------------------------------------------------------
from pmagent.agents.router import route_to_lane  # noqa: E402
from pmagent.schemas import RouteDecision  # noqa: E402


def test_every_route_in_the_schema_has_a_destination():
    # A route the classifier can return but the map doesn't know raised
    # KeyError mid-conversation and killed the graph.
    routes = RouteDecision.model_fields["route"].annotation.__args__
    for route in routes:
        assert route_to_lane(route)


def test_every_lane_a_route_reaches_exists():
    from pmagent.agents.router import ROUTE_TO_LANE
    from pmagent.assistant import PMAssistant

    lanes = set(PMAssistant(model=object()).lanes) | {"requirements"}
    assert set(ROUTE_TO_LANE.values()) <= lanes


def test_unknown_route_falls_back_to_the_read_only_lane():
    assert route_to_lane("nonsense") == "query_agent"


def test_requirements_route_reaches_the_prd_lane():
    assert route_to_lane("requirements") == "requirements"


def test_finance_route_reaches_the_finance_lane():
    assert route_to_lane("finance") == "finance_agent"


def test_the_finance_route_description_names_what_lands_there():
    # The description string is the classifier's only knowledge of a route.
    description = RouteDecision.model_fields["route"].description
    assert "FY budget" in description
    assert "fy_start" in description


# ---------------------------------------------------------------------------
# Route stickiness across a multi-turn workflow
# ---------------------------------------------------------------------------
from pmagent.agents.router import classify, is_continuation, recent_context  # noqa: E402
from pmagent.messages import assistant_message, user_message  # noqa: E402


def _tool_call_turn(*names_):
    return {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": f"t{i}", "name": n, "input": {}}} for i, n in enumerate(names_)
    ]}


def _tool_result_turn(text_, image=False):
    content = [{"text": text_}]
    if image:
        content.append({"image": {"format": "png", "source": {"bytes": b"png"}}})
    return {"role": "user", "content": [
        {"toolResult": {"toolUseId": "t0", "status": "success", "content": content}}
    ]}


def test_confirmations_are_recognised_as_continuations():
    for text in [
        "yes", "Yes", "yes please", "ok", "confirm", "confirmed",
        "confirm creation", "create it", "create them", "ok go ahead please",
        "go ahead", "proceed", "approve", "do it", "lgtm", "looks good",
        "no", "cancel", "stop", "reject", "no, stop",
    ]:
        assert is_continuation(text), text


def test_a_message_carrying_its_own_topic_is_not_a_continuation():
    # These must be classified afresh — they introduce new work.
    for text in [
        "what is the status of sprint 31?",
        "yes, also draft a ticket for FX",
        "yes, and also write a PRD from these notes about the barcode feed",
        "create a ticket to migrate the FX pipeline to Snowflake",
        "",
    ]:
        assert not is_continuation(text), text


def test_a_confirmation_keeps_the_lane_without_calling_the_model():
    # The bug this guards: "confirm creation" was classified on its own, matched
    # no route, fell back to the read-only query lane, and the agent reported it
    # had no create tool. A confirmation belongs to the workflow in progress.
    messages = [
        user_message("draft tickets for the WaveKey column change"),
        assistant_message("[6 drafts] Shall I create these in Jira?"),
        user_message("confirm creation"),
    ]
    # No model is passed and none is patched: staying on the previous route must
    # be a pure-Python path, so this passes without any model call at all.
    assert classify(messages, "ticket", model="no model call allowed") == "ticket"


class _FakeStructured:
    """Stands in for `llm.structured` inside the router."""

    def __init__(self, route):
        self.route = route
        self.prompts: list[str] = []

    def __call__(self, schema, prompt, **kwargs):
        assert schema is RouteDecision
        self.prompts.append(prompt)
        return RouteDecision(route=self.route)


def _patch_router(monkeypatch, route="query"):
    fake = _FakeStructured(route)
    monkeypatch.setattr("pmagent.agents.router.structured", fake)
    return fake.prompts


def test_an_unrecognised_confirmation_falls_through_to_a_context_aware_classifier(
    monkeypatch,
):
    # The message that actually broke was "conifrm creation" — a typo the word
    # guard can't and shouldn't try to match. It must reach the classifier, and
    # the classifier must be able to see what it's confirming.
    seen = _patch_router(monkeypatch, route="ticket")
    messages = [
        user_message("draft tickets for the WaveKey column change"),
        assistant_message("[6 drafts] Shall I create these in Jira?"),
        user_message("conifrm creation"),
    ]
    assert classify(messages, "ticket") == "ticket"

    prompt = seen[0]
    assert "Shall I create these in Jira?" in prompt
    assert "'ticket' route" in prompt


def test_a_tool_result_is_not_mistaken_for_the_users_request():
    # Strands tool results are role "user" messages. The confirmation after one
    # must still be read as the confirmation — the Strands form of the original's
    # injected-image guard (Confluence images now ride inside the tool result).
    messages = [
        user_message("draft a ticket for the employee dimension"),
        _tool_call_turn("read_confluence_page"),
        _tool_result_turn("Confluence image 1 of 1: dim.png", image=True),
        assistant_message("Here is the draft. Create it?"),
        user_message("yes"),
    ]
    assert classify(messages, "ticket", model="no model call allowed") == "ticket"


def test_rejections_also_stay_in_the_lane():
    messages = [assistant_message("Shall I create these?"), user_message("no")]
    assert classify(messages, "ticket", model="no model call allowed") == "ticket"


def test_a_confirmation_with_no_previous_route_is_still_classified(monkeypatch):
    # First message of a session continues nothing, so it must reach the model
    # rather than stick to an empty route.
    seen = _patch_router(monkeypatch, route="query")
    assert classify([user_message("yes")], "") == "query"
    assert len(seen) == 1


def test_the_classifier_sees_recent_conversation_not_just_the_last_message(monkeypatch):
    # Classifying "add 3 points to the second one" in isolation is hopeless; the
    # drafting turn before it is what makes it a ticket request.
    seen = _patch_router(monkeypatch, route="ticket")
    classify([
        user_message("draft tickets for the WaveKey column change"),
        assistant_message("Here are 6 drafts ..."),
        user_message("add 3 points to the second one"),
    ], "ticket")

    prompt = seen[0]
    assert "WaveKey column change" in prompt          # earlier turn is visible
    assert "add 3 points to the second one" in prompt  # and so is the new one
    assert "'ticket' route" in prompt                  # plus where we already are


def test_tool_only_assistant_turns_appear_in_the_context(monkeypatch):
    # An assistant turn with only tool calls has no text. "It just called
    # validate_ticket_drafts" is a strong routing signal, so it must not vanish.
    seen = _patch_router(monkeypatch, route="ticket")
    classify([
        _tool_call_turn("validate_ticket_drafts"),
        user_message("add story points to each of those"),
    ], "ticket")
    assert "validate_ticket_drafts" in seen[0]


def test_tool_results_are_labelled_as_tool_output_not_as_the_user():
    context = recent_context([
        user_message("check CSCI-1"),
        _tool_call_turn("read_jira_issues_by_key"),
        _tool_result_turn("CSCI-1 Done", image=True),
    ])
    assert "Tool result: CSCI-1 Done" in context
    assert "User: CSCI-1 Done" not in context
    assert "(called tools: read_jira_issues_by_key)" in context


def test_the_ticket_route_description_covers_continuations():
    # The classifier only knows what this description tells it.
    description = RouteDecision.model_fields["route"].description.lower()
    assert "confirm" in description
    assert "move" in description and "sprint" in description


# ---------------------------------------------------------------------------
# The approval prompt must expose provenance, not just content
# ---------------------------------------------------------------------------
from pmagent.cli.approval import (  # noqa: E402
    _describe_write,
    describe_write,
    unchecked_scope_warning,
)
from pmagent.messages import last_user_text  # noqa: E402


def _create_call(drafts):
    return {"name": "create_jira_issues", "args": {"drafts": drafts}}


def _draft(summary, **extra):
    return {"summary": summary, "description": "Because.", **extra}


def test_a_drafts_source_issue_appears_in_the_approval_prompt():
    """The batch that went wrong was nine per-issue drafts, one of which came
    from a card the user never named. Every other field on that draft was
    correct, so provenance is the only thing that distinguishes it.
    """
    described = _describe_write(
        _create_call([_draft("AU ingestion for X", source_issue="CSCI-1379")])
    )
    assert "CSCI-1379" in described


def test_a_large_batch_gets_a_scannable_manifest():
    drafts = [
        _draft(f"dbt model update || Model{i} AU ingestion", source_issue=f"CSCI-{1700 + i}")
        for i in range(9)
    ]
    described = _describe_write(_create_call(drafts))

    # Every source key is listed once in the manifest and once in its detail
    # block; what matters is that all nine are visible before the detail wall.
    header = described.split("1. [")[0]
    for i in range(9):
        assert f"CSCI-{1700 + i}" in header
    assert "9 draft(s), 9 distinct source issue(s)" in described


def test_a_manifest_flags_drafts_that_name_no_source():
    drafts = [_draft(f"S{i}", source_issue="CSCI-1") for i in range(3)]
    drafts.append(_draft("S3"))
    described = _describe_write(_create_call(drafts))

    assert "1 draft(s) name no source" in described
    assert "a source appears more than once" in described


def test_a_small_batch_skips_the_manifest():
    # Three drafts are readable as detail blocks; a table would be noise.
    described = _describe_write(_create_call([_draft("only one")]))
    assert "assignee" not in described.split("1. [")[0]


def test_a_renderer_crash_never_takes_down_the_approval_prompt(monkeypatch):
    # If rendering raised, the lane would stay paused on an interrupt nobody can
    # answer. The safe wrapper shows the raw call and the failure instead.
    import pmagent.cli.approval as approval

    def boom(call):
        raise RuntimeError("renderer bug")
    monkeypatch.setattr(approval, "_describe_write", boom)
    described = describe_write({"name": "create_fy_budget_csv", "args": {"fy_start": 202607}})
    assert "create_fy_budget_csv" in described and "renderer bug" in described


# ---------------------------------------------------------------------------
# Layer 3 of the scope reconciler — catching a write that skipped the check
# ---------------------------------------------------------------------------
def _create_with(args):
    return [{"name": "create_jira_issues", "id": "1", "args": args}]


def test_a_scopeless_batch_after_a_key_list_is_flagged():
    """The check only runs if the model passes `scope`. This notices when it didn't."""
    messages = [user_message("for CSCI-1712, CSCI-1714 and CSCI-1716 make cards")]
    warning = unchecked_scope_warning(messages, _create_with({"drafts": [{}, {}, {}]}))

    assert warning is not None
    assert "declared no scope" in warning
    assert "CSCI-1712" in warning


def test_a_batch_that_declared_scope_is_not_flagged():
    messages = [user_message("for CSCI-1712, CSCI-1714 and CSCI-1716 make cards")]
    warning = unchecked_scope_warning(
        messages, _create_with({"drafts": [{}], "scope": ["CSCI-1712"]})
    )
    assert warning is None


def test_a_message_naming_no_keys_is_not_flagged():
    # The heuristic must stay quiet on ordinary requests, or it gets ignored.
    messages = [user_message("draft a ticket for the FX backfill")]
    assert unchecked_scope_warning(messages, _create_with({"drafts": [{}]})) is None


def test_tool_results_are_not_read_as_the_user_speaking():
    """A tool result is a role-"user" message in Strands, but not the user."""
    messages = [
        user_message("draft a ticket for the FX backfill"),
        _tool_call_turn("query_jira_issues"),
        _tool_result_turn("CSCI-1712 CSCI-1714 CSCI-1716"),
    ]
    assert "FX backfill" in last_user_text(messages)
    assert unchecked_scope_warning(messages, _create_with({"drafts": [{}]})) is None


def test_a_non_create_write_is_never_flagged():
    messages = [user_message("move CSCI-1712, CSCI-1714, CSCI-1716 to sprint 31")]
    calls = [{"name": "move_jira_issues_to_sprint", "id": "1", "args": {}}]
    assert unchecked_scope_warning(messages, calls) is None
