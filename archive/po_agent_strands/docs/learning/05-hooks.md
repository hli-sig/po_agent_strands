# 5. Hooks

## The concept

Hooks let you run code at fixed points of the agent loop, without touching the
loop. You write a `HookProvider` and register callbacks for event types:

```python
from strands.hooks import HookProvider, MessageAddedEvent, BeforeToolCallEvent

class Tracer(HookProvider):
    def register_hooks(self, registry, **kwargs):
        registry.add_callback(MessageAddedEvent, self.on_message)
        registry.add_callback(BeforeToolCallEvent, self.on_tool)

agent = Agent(..., hooks=[Tracer()])
```

The events you'll use most, in the order they fire during one `agent(...)` call.
`MessageAddedEvent` fires throughout, whenever a message is appended:

| Event | When | Can change |
|-------|------|-----------|
| `BeforeInvocationEvent` | start of `agent(...)` | `cancel` the invocation, replace the input `messages` |
| `BeforeModelCallEvent` | before each model call | `cancel` it |
| `AfterModelCallEvent` | after each model call | `retry` it |
| `BeforeToolsEvent` | the model asked for a *batch* of tools; none has run yet | `cancel` the batch; can **interrupt** |
| `BeforeToolCallEvent` | before *one* tool runs | `cancel_tool`, swap `selected_tool`, edit `tool_use`; can **interrupt** |
| `AfterToolCallEvent` | after one tool ran | replace `result`, `retry` |
| `AfterToolsEvent` | after the batch | `end_turn` |
| `AfterInvocationEvent` | end of `agent(...)` | `resume` (start another invocation with new input) |
| `MessageAddedEvent` | any message appended to `agent.messages` | — |

Hooks come in two kinds:
- **Observers** log, print or trace.
- **Controllers** cancel, rewrite, retry or pause. The next lesson builds a
  controller.

`callback_handler` vs hooks: the callback handler sees *stream chunks* (token
deltas). Hooks see *whole lifecycle events*. For a UI that prints whole messages and
tool results, hooks are the right level.

## In this repo

- `pmagent/cli/echo.py::ConsoleEcho` is an observer on `MessageAddedEvent`. It
  prints assistant text, `-> tool(args)` lines and `| result` previews, and skips
  the user's own prompt. Every lane agent gets it (`main.py::build_assistant`). The
  stateless structured-output agents don't, which is why their hidden tool calls
  never appear on screen.
- `pmagent/gate.py::ApprovalGate` is a controller on `BeforeToolsEvent`. See
  lesson 6.
- `pmagent/agents/common.py::make_lane_agent` puts `ApprovalGate()` first in every
  lane's hook list. No lane can be built without it.

## How the LangGraph original did it

- Printing: `main.py` diffed the state's message list after every graph step
  (`_print_new_messages` with a `seen` set).
- Gating: done with graph structure (`interrupt_before` on a write node).
- Strands expresses both as hooks on the one agent loop.

## Exercise

1. Run `05_hooks.py`. Add a callback on `AfterModelCallEvent` that prints
   `event.stop_response.message["content"]` if present.
2. Write an observer that counts tool calls per tool name and prints the totals at
   `AfterInvocationEvent`. Add it to `main.py::build_assistant` next to
   `ConsoleEcho()`, then **live**: use the CLI for a few turns through the
   write-blocking launcher, `uv run scripts/smoke.py`. Offline alternative: add it
   to `hooks=[...]` in `05_hooks.py`.
3. Why is `ConsoleEcho` a hook and not code in `PMAssistant`? (Hint: the
   requirements lane appends messages *outside* any agent. Look at
   `pmagent/assistant.py::TurnResult.reply`.)
