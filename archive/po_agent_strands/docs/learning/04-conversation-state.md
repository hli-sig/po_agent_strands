# 4. Conversation state

## The concept

A Strands conversation is just `agent.messages`: a list of plain dicts in the Bedrock
Converse shape.

```python
{"role": "user",      "content": [{"text": "How is sprint 31?"}]}
{"role": "assistant", "content": [{"text": "Checking."},
                                  {"toolUse": {"toolUseId": "t1", "name": "get_sprint_status_by_number",
                                               "input": {"sprint_number": 31}}}]}
{"role": "user",      "content": [{"toolResult": {"toolUseId": "t1", "status": "success",
                                                  "content": [{"text": "60.7% complete"}]}}]}
{"role": "assistant", "content": [{"text": "Sprint 31 is 60.7% complete."}]}
```

Three things to internalise:

1. **Tool results are `role: "user"` messages.** "The last user message" is not "the
   last thing the human typed". `pmagent/messages.py::last_user_text` skips
   tool-result messages for exactly this reason. Getting this wrong makes the
   router classify tool output as if the user had said it.
2. **One tool round-trip is two messages**: the assistant message with N `toolUse`
   blocks, then one user message with N `toolResult` blocks. Every `toolUse` must be
   answered before the next user text, or the provider rejects the history.
3. **It's plain data.** You can read it, copy it, and hand it to another agent
   (`agent.messages = shared`). That is how this repo gives its six lane agents one
   memory. The requirements lane is not an agent; it appends its exchange to the
   same list directly.

### Conversation managers

As a conversation grows, something has to decide what to keep. Strands' default is a
`SlidingWindowConversationManager`: it drops the oldest messages when needed.
Alternatives are `SummarizingConversationManager` and `NullConversationManager`
(keep everything).

This repo uses `NullConversationManager` deliberately
(`pmagent/agents/common.py::make_lane_agent`). A ticket batch is checked against the
list of keys the user typed at the start. A sliding window could silently drop that
list mid-session, and the scope check would then compare drafts against nothing.
The trade-off is that a very long session overflows the context window. The CLI
catches `ContextWindowOverflowException` and tells you to `/new`
(`main.py::handle_turn`).

For persistence across process restarts, Strands has session managers
(`FileSessionManager`, `S3SessionManager`). The original only had in-memory state,
so this port doesn't use them. Adding one would be a good exercise.

## In this repo

- `pmagent/messages.py`: small readers over the message shape (`text_of`,
  `tool_uses`, `tool_results`, `last_user_text`).
- `pmagent/assistant.py::PMAssistant._run_lane`: `agent.messages = self.messages`
  before a lane runs. Every lane appends to the one list.
- `pmagent/assistant.py::_close_unanswered_tool_calls`: when an approval is
  abandoned, the pending `toolUse` still needs a `toolResult` (point 2 above), so
  this adds an explicit "Not executed" result.
- `pmagent/agents/router.py::recent_context`: renders the tail for the classifier,
  labelling tool results as `Tool result:`, never `User:`.

## How the LangGraph original did it

`PMState.messages` was a list of LangChain message objects (`HumanMessage`,
`AIMessage`, `ToolMessage`), merged by the `add_messages` reducer and persisted by
a `MemorySaver` checkpointer. It was one object threaded through the graph. Strands
has no graph state: the conversation is the agent's list, and sharing it is an
explicit assignment.

## Exercise

1. Run `04_shared_history.py`, then remove the `lane.messages = shared` line in a
   copy and run it again. Offline, the scripted replies don't change, so look at the
   **message count**: 4 becomes 2, and the ticket lane never saw the first
   exchange. **live** With `--live`, the real model's answer shows it too.
2. In a Python shell, build a `PMAssistant` with a `ScriptedModel` (lesson 9 covers
   it). Pass `classify=lambda messages, previous: "query"` so the router doesn't
   consume a scripted reply (see `tests/test_assistant.py::make`). Send two turns
   and print `assistant.messages`, then find the toolUse/toolResult pairs.
3. Add `SlidingWindowConversationManager(window_size=4)` in `make_lane_agent`, run
   `uv run pytest -q`, and see what breaks. Then revert it.
