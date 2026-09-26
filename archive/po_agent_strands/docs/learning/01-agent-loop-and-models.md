# 1. The agent loop and models

## The concept

In Strands an **Agent** is four things:

```
Agent = model + system prompt + tools + conversation (agent.messages)
```

Calling it runs **the agent loop** for you:

```
agent("How is sprint 31?")
  └─ model call ──► assistant message
        ├─ has toolUse blocks? ─► run the tools ─► append toolResults ─► model call again ─┐
        │                                                                                  │
        └─ no tools requested ─► stop_reason="end_turn", return AgentResult  ◄─────────────┘
```

You never write that loop. That is the single biggest difference from LangGraph,
where every *lane* (one of the app's specialists; see the glossary) wired it by
hand: an LLM node, a `ToolNode` (the node that runs tools), and a conditional edge
back to the LLM.

```python
from strands import Agent
agent = Agent(model=my_model, system_prompt="You are a PM assistant.", tools=[...],
              callback_handler=None)
result = agent("Say hello")
result.stop_reason        # "end_turn" | "interrupt" | ...  (a reply cut off by the
                          # token limit raises MaxTokensReachedException instead)
str(result)               # final text
result.structured_output  # lesson 3
result.interrupts         # lesson 6
agent.messages            # the whole conversation, as plain dicts (lesson 4)
```

**Models are adapters.** `strands.models` has one class per provider:
`AnthropicModel`, `OpenAIModel` (chat completions), `OpenAIResponsesModel`,
`BedrockModel` (the default if you pass none), `LiteLLMModel`, `OllamaModel`, … They
all implement the same `Model.stream()` interface, so switching provider is a
one-object change.

**`callback_handler`.** By default an agent streams its output to stdout
(`PrintingCallbackHandler`). That's handy in a notebook and wrong in an app with its
own UI. So the CLI and the stateless helper agents pass `callback_handler=None` and
print through hooks instead (lesson 5). The web server passes its own handler,
which streams text to the browser (lesson 11).

## In this repo

- `pmagent/llm.py::build_model` is the only place a provider is chosen;
  `pmagent/llm.py::get_model` caches its result for the CLI (the web server builds
  one per conversation for Anthropic, lesson 10). Note the two real-world traps it
  documents:
  - gpt-5.x on chat completions **rejects function tools** unless
    `reasoning_effort="none"` is sent. That was verified live: without it you get
    HTTP 400. Every lane has tools, and structured output is itself a tool, so
    this setting is mandatory.
  - Any real reasoning effort needs `OpenAIResponsesModel`.
- `pmagent/agents/common.py::make_lane_agent` builds every lane's agent. It is one
  constructor call. Compare it with the original's `make_agent_node` plus
  `make_tools_router` plus the `_add_agent_lane` wiring in `graph.py`.
- The lanes themselves (`pmagent/agents/ticket_agent.py`,
  `pmagent/agents/sprint_agent.py`, …) are still just a `SYSTEM_PROMPT` and a
  `TOOLS` list. The declarative lane idea survived the port unchanged; only the
  wiring disappeared.

## How the LangGraph original did it

`get_llm()` returned a LangChain chat model. Each lane then called
`.bind_tools(tools)` on it, wrapped it in a node function, added a `ToolNode`, and
added edges `agent → tools → agent` plus a router that ends the turn when there are
no tool calls. Strands folds all of that into `Agent(...)`.

## Exercise

1. Run `uv run docs/learning/examples/01_hello_agent.py`, then (**live**) run it with `--live`.
   Compare the `agent.messages` dump with what you expected.
2. In a copy of `01_hello_agent.py`, delete `callback_handler=None` and run it.
   Offline, the scripted reply is echoed to stdout; **live** (`--live`), you see a
   real model's tokens stream. What gets printed, and why would that break
   `main.py`'s output?
3. **live** Switch provider: set `LLM_PROVIDER` in `.env` to the one you *aren't*
   using (`anthropic` is the default, `openai` the alternative), add that
   provider's key, and run the CLI through the write-blocking launcher,
   `uv run scripts/smoke.py`. No other code changes. Which function made that
   possible?
