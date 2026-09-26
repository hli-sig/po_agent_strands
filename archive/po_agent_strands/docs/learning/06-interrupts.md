# 6. Interrupts: human in the loop

## The concept

An **interrupt** pauses an agent from inside a hook, hands control back to your
code, and resumes exactly where it stopped once you answer.

```python
class Gate(HookProvider):
    def register_hooks(self, registry, **kwargs):
        registry.add_callback(BeforeToolsEvent, self.check)

    def check(self, event):
        writes = [b["toolUse"] for b in event.message["content"]
                  if "toolUse" in b and b["toolUse"]["name"] in WRITES]
        if not writes:
            return
        answer = event.interrupt("approval", reason=writes)  # 1st time: raises
        if answer != "yes":
            event.cancel = "The user rejected this. Nothing was written."

result = agent("Comment 'nudge' on CSCI-1")
result.stop_reason        # "interrupt": no tool has run
result.interrupts         # [Interrupt(id=..., name="approval", reason=[...])]

result = agent([{"interruptResponse": {"interruptId": i.id, "response": "yes"}}
                for i in result.interrupts])   # resume
```

The sequence:

1. The model asks for tools. `BeforeToolsEvent` fires and the hook calls
   `event.interrupt(...)`.
2. Strands stops the loop and returns `stop_reason="interrupt"`. **No tool has
   executed.** The agent is now in an interrupt state and accepts only
   `interruptResponse` input.
3. You show `reason` to a human and call the agent with their answer.
4. Strands re-fires `BeforeToolsEvent` for the same batch. This time
   `event.interrupt(...)` **returns** the answer.
5. Approve: the batch runs. Reject: `event.cancel = "<text>"` gives every call in
   the batch an error result with that text, and the model responds to it.
6. The next write batch interrupts again. Answers are not reused across tool
   cycles.

### Why `BeforeToolsEvent` and not `BeforeToolCallEvent`?

Both can interrupt. The batch event lets you ask **once** about a whole batch, and
gate a harmless read that was bundled with a write, so a write can never slip
through because it was bundled with a search.

### Strands' built-in alternative

`strands.vended_interventions.hitl.HumanInTheLoop` does per-tool approval
out of the box. Register it with `Agent(interventions=[...])`, not `hooks=[...]`:
- `allowed_tools=[...]` run freely and everything else asks.
- It has an `ask="stdio"` mode and an optional LLM risk classifier.

It is fail-closed by default, which is good. This repo builds its own gate for two
reasons. The approval prompt needs repo-specific rendering (draft manifests, scope
warnings). And writing one teaches the mechanism. For a new project, start with
`HumanInTheLoop`.

## In this repo

- `pmagent/gate.py::ApprovalGate.check_batch` is the gate.
- `pmagent/gate.py::requires_approval` decides which calls are gated:
  1. A tool the agent doesn't have can't run, so it is never gated.
  2. A known write is gated.
  3. Anything not declared a read is gated (**fail closed**, see lesson 8).
- `pmagent/assistant.py::PMAssistant.resume` answers the interrupt on the same
  agent. `PMAssistant.discard_pending` handles the case nobody answers. A Strands
  agent with an unanswered interrupt refuses any other input, and its interrupt
  state is private, so the paused agent is replaced and the dangling tool call is
  closed out.
- `main.py::resolve_approvals` is the terminal side: render with
  `pmagent/cli/approval.py::describe_write`, ask `y/N`, resume.
- Evidence: `tests/test_gate.py` drives the real loop with spy tools, and both
  smoke transcripts in `docs/evidence/` show it live.

## How the LangGraph original did it

- The original compiled the graph with `interrupt_before=[<lane>_write_tools]`,
  which physically halts before the write node.
- Rejection was injected with
  `graph.update_state(config, {"messages": [ToolMessage(...)]}, as_node=...)`.
- It gave the same guarantee as the Strands gate. It was more wiring (split
  read/write ToolNodes per lane), and it **failed open**: a tool not in
  `WRITE_TOOL_NAMES` went to the ungated node.

## Exercise

1. Run `06_interrupt_gate.py --ask` and try both answers. Then (**live**)
   `--live --ask`.
2. In the example, move the gate to `BeforeToolCallEvent` (use `event.tool_use`
   and `event.cancel_tool`). Make the model call a read and a write in one turn.
   What changes?
3. Replace the example's `Gate` with Strands' built-in one. It is an
   *intervention*, not a hook, so it goes in a different argument:
   `Agent(..., interventions=[HumanInTheLoop(allowed_tools=[], ask="stdio")])`
   (drop `hooks=[Gate()]`). What do you gain, and what do you lose compared with
   `describe_write`?
4. Prove the gate is tested: do lesson 9's exercise 2 now (break
   `ApprovalGate.check_batch` on purpose, run `uv run pytest tests/test_gate.py`,
   count the failures, then revert).
