"""The model layer and the MCP client: which provider class is built, and how an
optional MCP server is started without ever taking the app down.

No network: provider objects are only constructed, never called, and the MCP
client is replaced by a fake.
"""

from __future__ import annotations

import pytest
import strands.tools.mcp as strands_mcp
from strands import Agent

from pmagent import env, llm
from pmagent.tools import mcp_tools
from tests.fakes import ScriptedModel, text


@pytest.fixture
def provider(monkeypatch):
    def use(name, effort="none", summary=""):
        monkeypatch.setattr(env, "LLM_PROVIDER", name)
        monkeypatch.setattr(env, "LLM_REASONING_EFFORT", effort)
        monkeypatch.setattr(env, "LLM_REASONING_SUMMARY", summary)
        monkeypatch.setattr(env, "ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(env, "OPENAI_API_KEY", "test-key")
    return use


def test_anthropic_builds_the_anthropic_model(provider):
    provider("anthropic")
    model = llm.build_model()
    assert type(model).__name__ == "AnthropicModel"
    assert model.get_config()["max_tokens"] == env.LLM_MAX_TOKENS


def test_openai_without_reasoning_uses_chat_completions_with_effort_none(provider):
    # gpt-5.x rejects temperature unless reasoning_effort is "none", so the two go together.
    provider("openai")
    model = llm.build_model()
    assert type(model).__name__ == "OpenAIModel"
    assert model.get_config()["params"] == {"reasoning_effort": "none", "temperature": 0.1}
    assert llm.reasoning_available() is False


def test_openai_with_reasoning_uses_the_responses_api(provider):
    provider("openai", effort="high", summary="detailed")
    model = llm.build_model()
    assert type(model).__name__ == "OpenAIResponsesModel"
    assert model.get_config()["params"] == {"reasoning": {"effort": "high", "summary": "detailed"}}
    assert llm.reasoning_available() is True


def test_an_unknown_provider_is_refused(provider):
    provider("llama")
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER='llama'"):
        llm.build_model()


def test_invocation_usage_reports_one_calls_tokens_and_model_calls():
    agent = Agent(model=ScriptedModel([[text("hi")]]), callback_handler=None)
    usage = llm.invocation_usage(agent("hello"))
    assert set(usage) == {"input_tokens", "output_tokens", "total_tokens", "model_calls"}
    assert usage["model_calls"] == 1


class _Page(list):
    def __init__(self, items, token):
        super().__init__(items)
        self.pagination_token = token


class FakeClient:
    """Stands in for strands' MCPClient: two pages of tools, or a failing start."""

    instances: list = []

    def __init__(self, url=None, headers=None, fail=False):
        self.url, self.headers, self.fail = url, headers, fail
        self.started = self.stopped = False
        self.tokens = []
        FakeClient.instances.append(self)

    def start(self):
        if self.fail:
            raise ConnectionError("unreachable")
        self.started = True

    def list_tools_sync(self, pagination_token=None):
        self.tokens.append(pagination_token)
        return _Page(["a", "b"], "next") if pagination_token is None else _Page(["c"], None)

    def stop(self, *exc):
        self.stopped = True


def test_start_lucid_follows_every_page_of_tools(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(strands_mcp, "MCPClient", FakeClient)
    monkeypatch.setattr(env, "LUCID_MCP_AUTH_TOKEN", "tok")
    session = mcp_tools.start_lucid()
    client = FakeClient.instances[0]
    assert session.tools == ["a", "b", "c"] and client.tokens == [None, "next"]
    assert client.headers == {"Authorization": "Bearer tok"}
    session.close()
    assert client.stopped


def test_start_lucid_failing_is_a_warning_and_none_not_a_crash(monkeypatch, capsys):
    FakeClient.instances = []
    monkeypatch.setattr(strands_mcp, "MCPClient", lambda **kw: FakeClient(fail=True, **kw))
    assert mcp_tools.start_lucid() is None
    assert "Lucid MCP unavailable (ConnectionError: unreachable)" in capsys.readouterr().out
    assert FakeClient.instances[0].stopped          # no half-started thread is left behind


def test_no_mcp_tool_is_approved_as_a_read_until_a_human_reviews_it():
    assert mcp_tools.APPROVED_READ_TOOLS == frozenset()
