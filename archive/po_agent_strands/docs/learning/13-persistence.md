# 13. Persistence: surviving a restart

> **Part 3: Extend (guided build).** Needs lesson 12 (the `conversation_id` and
> `turn_id` it adds to `PMAssistant`). Design: `docs/PLAN_HARNESS_MEMORY.md` §3
> ("Phase M1").

## The concept

Restart `uv run main.py` or `web.py` today and every conversation is gone,
because `PMAssistant.messages` lives only in memory. The goal is to keep
conversations across restarts, with one hard rule about writes:

| State at restart | What happens | Why |
|------------------|--------------|-----|
| a finished conversation | reloaded as it was | the easy part |
| an approval **waiting** for y/n | closed as **abandoned**: "Not executed — the approval was abandoned before the user answered." | a paused Strands agent can't be recreated, and the human never said yes |
| an approval **approved and executing** when the process died | closed as **interrupted**: "Outcome unknown — the process stopped during this write. Verify in Jira before retrying." | the write *may* have happened. Saying "nothing was written" would invite a duplicate ticket |

The second and third rows are the lesson. **Persistence of an agent is mostly
about the in-flight states, not the happy path.**

## The Strands mechanism: snapshots

A Strands agent can capture its state as a plain, JSON-able `Snapshot`, and a
fresh agent can load it:

```python
snap = agent.take_snapshot(preset="session", app_data={"route": "query"})
snap.schema_version        # "1.0"
snap.data                  # messages, state, conversation_manager_state, interrupt_state, ...
snap.app_data              # yours: anything the app needs back

fresh = Agent(model=...)
fresh.load_snapshot(snap)  # fresh.messages is the old history
```

```bash
uv run docs/learning/examples/13_snapshots.py
```

It prints the snapshot's fields, then a brand-new agent answers from the restored
history. Strands also ships `strands.session.SnapshotSessionManager` (and the
older `FileSessionManager`). Either saves an agent automatically after each
invocation.

**Why PO Agent can't use a session manager directly.** A session manager saves
*one agent*. PO Agent has six lane agents sharing **one** message list, plus the
requirements workflow, which appends messages with no agent at all (lesson 7).
Per-agent saving would store fragments, and would never store requirements
turns. So you save the **shared list** yourself, in the **same shape** as a
Strands snapshot: `schema_version`, `messages`, and `app_data={route, …}`. Bytes
(Confluence images) go through
`strands.types.session.encode_bytes_values` / `decode_bytes_values`, so they
survive JSON.

## What you build

1. `➕ pmagent/persistence.py::ConversationStore`: standard-library `sqlite3`.
   - The file is `conversations.sqlite3` in lesson 12's `data_dir()` (so
     `PMAGENT_DATA_DIR` moves it in tests), mode 0600. Add
     `data/conversations.sqlite3*` to `.gitignore`; the `*` covers the
     `-wal`/`-shm` files.
   - Enable WAL (`PRAGMA journal_mode=WAL`) and use one connection per thread
     (`threading.local()`), because web turns run on worker threads.
   - Tables:
     - `conversations(id PRIMARY KEY, created_at, updated_at, snapshot_json)`;
     - `approvals(conversation_id, tool_use_id, name, status, updated_at)`, with
       status `pending`|`approved/executing`|`done`|`rejected`|`abandoned`|`interrupted`;
     - (web) `turns(conversation_id, turn_id, events_json)`.
   - Methods: `save`, `load`, `list`, `delete`, `set_approval`, and
     `open_approvals(conversation_id)`.
2. **Plain-data round trip on `PMAssistant`:**
   - `snapshot() -> dict` returns `{"schema_version": "1.0",
     "messages": encode_bytes_values(self.messages), "app_data": {"route":
     self.route, "conversation_id": …}}`.
   - `restore(data)` does the reverse, and clears `_agents`, so lanes are rebuilt
     and pointed at the restored list on their next run.
3. **Persistence callbacks in `PMAssistant`**, so the CLI and web get the same
   guarantees. The frontend wires them to the store.
   - `on_pending(calls)`: called in `_run_lane` when the lane pauses, **before**
     `send()`/`resume()` returns. The store saves the snapshot (it now holds the
     `toolUse` message) and writes one `pending` approval row per call.
   - `on_before_resume(calls, approved)`: called at the top of `resume()`,
     **before** `_run_lane`. When approved, the store writes
     `approved/executing`; when rejected, `rejected`.
   - When the resume returns, the frontend saves the snapshot and marks the
     executed calls `done`.
   - After every `send()` that completes with nothing pending, the frontend saves
     the snapshot.
4. **On load, close the in-flight calls.** For each open approval:
   - `pending` → append a `toolResult` with the abandoned text, and set status
     `abandoned`;
   - `approved/executing` → append a `toolResult` with the "Outcome unknown"
     text, and set status `interrupted`.

   `pmagent/assistant.py::_close_unanswered_tool_calls` does this for the last
   assistant message, with one fixed text for every call. Generalise it to take
   **a text per `toolUseId`** (a dict), because one batch can hold both kinds.
5. **Log both** as `approval` run-log events (lesson 12), with the decision
   `abandoned` or `interrupted`.
6. **Frontends.**
   - CLI (`main.py`): `/sessions` (list), `/resume <id>`, `/delete <id>`, and
     `/new` (which saves the old conversation first).
   - Web: `SessionStore` in `pmagent/web/sessions.py` writes through on every
     state change and loads on start. The existing
     `DELETE /api/v1/conversations/{id}` now deletes the stored row too.
7. Tests: lesson 12's conftest already points `PMAGENT_DATA_DIR` at a temp dir
   for the whole session. Store tests set it to `tmp_path` with `monkeypatch`.

## Definition of done: the tests to write

`➕ tests/test_persistence.py`, offline:

- [ ] **Round trip:** a history with `toolUse`, `toolResult` **and an image
  block** saves, then loads equal. Build the image with `bytes`, as
  `tests/test_confluence_tools.py` does.
- [ ] **Pending, then restart:** `send()` pauses on `add_jira_comment`; save;
  build a *new* `PMAssistant` and restore. The call is closed as abandoned, the
  approval status is `abandoned`, and a new `send()` works (the lane isn't
  wedged).
- [ ] **Approved, then crash, then restart:** simulate the process dying
  mid-write. Give the test assistant an `on_before_resume` that calls the real
  store callback and then raises `RuntimeError("crash")`, so the store says
  `approved/executing` but the write never finishes. Restore into a new
  assistant. The call is closed with the "Outcome unknown" text and the status is
  `interrupted`.
- [ ] **Web:** two `create_app` instances sharing one db file. A conversation
  created in the first is readable from the second.
- [ ] **Concurrency:** two threads saving different conversations 100 times each
  cause no errors, and both rows are complete.
- [ ] **Delete** removes the row.
- [ ] **Mode 0600** on the db file.

## Traps

- **Don't try to persist a paused agent.** Its interrupt state is private and
  tied to that object (lesson 6). The plan deliberately drops "resume a paused
  write across restarts" (§11 out of scope).
- **Order matters for "interrupted".** If you write `approved/executing`
  *after* calling `resume`, a crash in the write leaves `pending` in the store,
  and you'd tell the user "Nothing was written" when it may have been. Write
  first, then act.
- **Don't store only the last N messages.** The shared list is the conversation.
  Lanes use `NullConversationManager` (lesson 4), so nothing trims it for you
  either. Size is fine for a local tool.
- SQLite objects are not shareable across threads. Use one connection per
  thread.

## Exercises

1. **(code)** Implement `snapshot()`/`restore()` first, with no database. Make
   the round-trip test pass, then add SQLite.
2. **(run)** In your build's CLI, with **`uv run scripts/smoke.py`** if your
   `.env` is a real Jira: ask for a comment, and when the approval prompt
   appears, kill the process **from another terminal** (`kill -9 <pid>`; find it
   with `pgrep -f smoke.py`). Ctrl-C won't do: `resolve_approvals` catches it and
   calls `discard_pending()` itself, so there'd be nothing to recover. Restart,
   `/resume` the conversation, and read the abandoned result in the history.
3. **(think)** Why is "Outcome unknown — verify in Jira" better than retrying the
   write automatically on restart?

**How the original did it:** LangGraph's `MemorySaver` checkpointer, in memory
only, so a restart lost everything there too. A persistent checkpointer was on the
original harness doc's list but never built.
