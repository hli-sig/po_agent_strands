"""The CLI layer: turn handling, the approval loop, and what gets echoed."""

from __future__ import annotations

from strands.types.exceptions import ContextWindowOverflowException

import main
from pmagent.assistant import PMAssistant, TurnResult
from pmagent.cli.echo import render_message
from pmagent.messages import user_message
from tests.fakes import ScriptedModel, call, text


class StubAssistant:
    """Just enough PMAssistant for main.handle_turn."""

    def __init__(self, send=None, resumes=()):
        self._send = send
        self._resumes = list(resumes)
        self.messages = [user_message("hello")]
        self.discarded = 0
        self.answers = []

    def send(self, text):
        return self._send(text)

    def resume(self, approved):
        self.answers.append(approved)
        return self._resumes.pop(0)

    def discard_pending(self):
        self.discarded += 1


def test_a_context_overflow_tells_the_user_to_start_over(capsys):
    def overflow(text):
        raise ContextWindowOverflowException("too long")

    stub = StubAssistant(send=overflow)
    assert main.handle_turn(stub, "hi") is True  # the CLI keeps running
    out = capsys.readouterr().out
    assert "too long for the model's context window" in out and "/new" in out
    assert stub.discarded == 1


def test_any_other_error_is_reported_and_any_pending_approval_closed(capsys):
    def boom(text):
        raise ValueError("bad turn")

    stub = StubAssistant(send=boom)
    assert main.handle_turn(stub, "hi") is True
    assert "ValueError: bad turn" in capsys.readouterr().out
    assert stub.discarded == 1


def test_the_approval_loop_describes_each_write_and_resumes_with_the_answer(capsys):
    pending = [{"id": "1", "name": "add_jira_comment",
                "args": {"issue_keys": ["CSCI-1"], "comment": "chasing this"}}]
    stub = StubAssistant(
        send=lambda t: TurnResult("ticket", pending=pending),
        resumes=[TurnResult("ticket")],
    )
    assert main.handle_turn(stub, "comment", ask=lambda prompt: "y") is True

    out = capsys.readouterr().out
    assert "APPROVAL REQUIRED" in out
    assert "Comment on 1 issue(s): CSCI-1" in out and "chasing this" in out
    assert stub.answers == [True]


def test_anything_but_yes_is_a_rejection():
    pending = [{"id": "1", "name": "assign_jira_issue", "args": {"issue_key": "CSCI-1", "assignee": "x"}}]
    for answer in ("", "n", "no", "maybe", "Y E S"):
        stub = StubAssistant(send=lambda t: TurnResult("ticket", pending=pending),
                             resumes=[TurnResult("ticket")])
        main.handle_turn(stub, "assign", ask=lambda prompt, a=answer: a)
        assert stub.answers == [False], answer


def test_ctrl_d_at_the_approval_prompt_quits_and_abandons_the_write():
    def eof(prompt):
        raise EOFError

    pending = [{"id": "1", "name": "assign_jira_issue", "args": {}}]
    stub = StubAssistant(send=lambda t: TurnResult("ticket", pending=pending))
    assert main.handle_turn(stub, "assign", ask=eof) is False
    assert stub.discarded == 1 and stub.answers == []


def test_a_requirements_reply_is_printed_by_the_cli(capsys):
    stub = StubAssistant(send=lambda t: TurnResult("requirements", reply="# My PRD"))
    main.handle_turn(stub, "notes")
    assert "# My PRD" in capsys.readouterr().out


def test_the_whole_cli_turn_with_a_real_assistant(capsys, monkeypatch):
    """handle_turn + PMAssistant + ConsoleEcho + a real Strands agent, model scripted."""
    from pmagent.cli.echo import ConsoleEcho

    model = ScriptedModel([
        [call("list_jira_transitions", issue_key="CSCI-1")],
        [text("CSCI-1 can move to Done.")],
    ])
    import pmagent.tools.jira.tools_read as tr

    class FakeJira:
        def list_transitions(self, key):
            return [{"name": "Done", "to_status": "Done"}]

    monkeypatch.setattr(tr, "get_client", lambda: FakeJira())
    assistant = PMAssistant(model=model, classify=lambda m, p: "query", hooks=[ConsoleEcho()])
    main.handle_turn(assistant, "where can CSCI-1 go?")

    out = capsys.readouterr().out
    assert '-> list_jira_transitions({"issue_key": "CSCI-1"})' in out
    assert "| CSCI-1 can move to:" in out
    assert "Agent\x1b[0m: CSCI-1 can move to Done." in out
    assert "where can CSCI-1 go?" not in out  # the user's own prompt isn't echoed


# -- echo rendering -------------------------------------------------------------


def test_the_users_own_prompt_is_not_echoed():
    assert render_message(user_message("hello")) == []


def test_images_in_tool_results_are_labelled_not_dumped():
    message = {"role": "user", "content": [{"toolResult": {
        "toolUseId": "1", "status": "success",
        "content": [{"text": "page text"}, {"image": {"format": "png", "source": {"bytes": b"\x89PNG" * 1000}}}],
    }}]}
    lines = render_message(message)
    joined = "\n".join(lines)
    assert "[image: png]" in joined and "PNG" not in joined.replace("[image: png]", "")


def test_long_tool_results_are_previewed():
    body = "x" * 2000
    message = {"role": "user", "content": [{"toolResult": {
        "toolUseId": "1", "status": "success", "content": [{"text": body}]}}]}
    (line,) = render_message(message)
    assert line.endswith(" ...\x1b[0m") and len(line) < 700


def test_a_real_context_overflow_reaches_the_cli_unwrapped(capsys):
    """Through a real PMAssistant and Strands loop: the model raises the overflow,
    and the CLI still recognises it (Strands wraps other model errors in
    EventLoopException — this one it must not)."""

    def overflow(messages):
        raise ContextWindowOverflowException("prompt is too long")

    assistant = PMAssistant(model=ScriptedModel([overflow]), classify=lambda m, p: "query")
    assert main.handle_turn(assistant, "hello") is True
    out = capsys.readouterr().out
    assert "too long for the model's context window" in out and "/new" in out
