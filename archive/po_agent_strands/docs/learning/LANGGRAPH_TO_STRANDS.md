# LangGraph → Strands: concept map for this codebase

Original: `/home/hl2/development/PO_Agent` (LangGraph 0.2+, LangChain core). This
repo: Strands Agents 1.57.

| Concern | LangGraph original | Strands rewrite | Lesson |
|---------|--------------------|-----------------|--------|
| Model | `llm.get_llm()` → `ChatAnthropic` / `ChatOpenAI` | `pmagent/llm.py::get_model` → `AnthropicModel` / `OpenAIModel` / `OpenAIResponsesModel` | 1 |
| Agent loop | per lane: LLM node + `ToolNode` + conditional edge (`make_agent_node`, `make_tools_router`, `_add_agent_lane`) | `Agent(model, system_prompt, tools)`, see `pmagent/agents/common.py::make_lane_agent` | 1 |
| Tool | `langchain_core.tools.tool` | `strands.tool` (drops prose after `Args:`) | 2 |
| Image tool results | `Command` + injected `HumanMessage(name=IMAGE_MESSAGE_NAME)` + `_is_injected` | `ToolResult` with `image` blocks; the OpenAI provider splits them itself | 2 |
| Structured output | `.with_structured_output(Schema).invoke(...)` | `agent(prompt, structured_output_model=Schema)`, see `pmagent/llm.py::structured` | 3 |
| State | `PMState` (Pydantic) + `add_messages` reducer + `MemorySaver` | `agent.messages` (list of dicts), shared by `pmagent/assistant.py::PMAssistant` | 4 |
| Typed slots (`prd`, `ticket_draft`…) | fields on `PMState` | return values (`pmagent/agents/requirements.py::RequirementsResult`), `PMAssistant.last_prd` | 4 |
| Printing | diff state after each graph step (`_print_new_messages`) | `MessageAddedEvent` hook, `pmagent/cli/echo.py::ConsoleEcho` | 5 |
| Write gate | `interrupt_before=[write nodes]`, split read/write `ToolNode`s, fails open on unknown tools | `BeforeToolsEvent` hook + `event.interrupt`, see `pmagent/gate.py::ApprovalGate`; fails closed | 6 |
| Reject | `graph.update_state(..., ToolMessage(...), as_node=...)` | `event.cancel = REJECTION_MESSAGE` | 6 |
| Resume | `graph.stream(None, config)` | `agent([{"interruptResponse": ...}])`, see `pmagent/assistant.py::PMAssistant.resume` | 6 |
| Routing | `classify_node` + `add_conditional_edges` + `route_from_classifier` | `pmagent/agents/router.py::classify` + `ROUTE_TO_LANE` dict | 7 |
| PRD loop | compiled `StateGraph` subgraph | plain-Python workflow (`pmagent/agents/requirements.py::run_requirements_loop`); `GraphBuilder` version in example 07 | 7 |
| MCP | `langchain-mcp-adapters` (async, never ran) | `strands.tools.mcp.MCPClient` (sync), see `pmagent/tools/mcp_tools.py::start_lucid` | 8 |
| Offline tests | fake `get_llm`, test graph wiring | `tests/fakes.py::ScriptedModel` drives the real agent loop | 9 |
| Tracing | LangSmith | OpenTelemetry (`strands.telemetry`), not wired here | — |
| Dev UI | `langgraph dev` (Studio) | our own web UI with a live thinking chain (`web.py`), built from hooks + the callback handler | 11 |
| Second frontend | "a second frontend can be added later" (never built) | `pmagent/web/` drives the same `PMAssistant` over HTTP: REST + SSE, the gate becomes an approvals resource | 10 |
| Streaming | `graph.stream(stream_mode="values")` | callback handler (`data`, `reasoningText`) + hooks → `pmagent/web/trace.py::TraceRecorder` | 11 |

## What did *not* change

All of `pmagent/tools/jira/` except the `@tool` import and docstrings, `adf.py`,
`fy_budget/`, all prompts and skills, the `READ_TOOLS`/`WRITE_TOOLS` contract, the
continuation vocabulary, the approval-prompt rendering (`describe_write`, the draft
manifest, the scope warning) and the PRD renderer. See
`docs/evidence/verbatim_report.txt`.
