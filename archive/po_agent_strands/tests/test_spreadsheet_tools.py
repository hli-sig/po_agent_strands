import base64
import json
import time

import pytest

from pmagent import env
from pmagent.tools import spreadsheet_tools as st
from pmagent.tools.spreadsheet_tools import apply_approved_requests, propose_cell_update


class FakeWorkbook:
    def __init__(self):
        self.cells = {("Control Page", "A160"): ""}
        self.rows = []
        self.writes = []

    def get_cell(self, sheet, cell):
        return self.cells[(sheet, cell)]

    def set_cell(self, sheet, cell, value):
        self.writes.append((sheet, cell, value))
        self.cells[(sheet, cell)] = value

    def append_request(self, row):
        self.rows.append(row)

    def requests(self):
        return list(enumerate(self.rows))

    def replace_request(self, index, row):
        self.rows[index] = row


def test_approved_control_page_update_is_applied():
    workbook = FakeWorkbook()
    request_id = propose_cell_update(
        workbook, "Control Page", "A160", "CODEX_ACCESS_TEST_2026-07-18"
    )
    workbook.rows[0][6] = "Approved"

    assert apply_approved_requests(workbook) == [request_id]
    assert workbook.writes == [
        ("Control Page", "A160", "CODEX_ACCESS_TEST_2026-07-18")
    ]
    assert workbook.rows[0][6] == "Applied"


def test_pending_update_never_changes_the_target_cell():
    workbook = FakeWorkbook()
    propose_cell_update(workbook, "Control Page", "A160", "CODEX_ACCESS_TEST_2026-07-18")

    assert apply_approved_requests(workbook) == []
    assert workbook.writes == []


# --- the rest of the approval workflow, with no network -----------------------


def test_a_proposal_records_the_value_it_expects_to_replace():
    workbook = FakeWorkbook()
    workbook.cells[("Control Page", "A160")] = "old"
    request_id = propose_cell_update(workbook, "Control Page", "A160", "new")
    row = workbook.rows[0]
    assert request_id.startswith("scr_") and row[0] == request_id
    assert row[2:7] == ["set_cell", "Control Page!A160", "old", "new", "Pending"]
    assert len(row) == len(st.QUEUE_HEADERS)


def test_an_approved_row_whose_target_changed_is_conflicted_not_applied():
    workbook = FakeWorkbook()
    propose_cell_update(workbook, "Control Page", "A160", "new")
    workbook.rows[0][6] = "Approved"
    workbook.cells[("Control Page", "A160")] = "someone else's edit"
    assert apply_approved_requests(workbook) == []
    assert workbook.writes == []
    assert workbook.rows[0][6] == "Conflicted" and workbook.rows[0][9] == "Target changed after proposal"


def test_only_approved_set_cell_rows_are_considered():
    workbook = FakeWorkbook()
    propose_cell_update(workbook, "Control Page", "A160", "x")
    workbook.rows[0][2], workbook.rows[0][6] = "delete_sheet", "Approved"
    assert apply_approved_requests(workbook) == [] and workbook.writes == []


def test_a_sheet_name_containing_an_exclamation_mark_still_splits_on_the_last_one():
    workbook = FakeWorkbook()
    workbook.cells[("Q1!Plan", "B2")] = ""
    propose_cell_update(workbook, "Q1!Plan", "B2", "done")
    workbook.rows[0][6] = "Approved"
    apply_approved_requests(workbook)
    assert workbook.writes == [("Q1!Plan", "B2", "done")]


def test_the_sharing_token_is_graphs_u_bang_base64url_without_padding():
    url = "https://contoso.sharepoint.com/:x:/r/sites/pm/Book.xlsx?d=1"
    token = st._sharing_token(url)
    assert token.startswith("u!") and "=" not in token and "/" not in token and "+" not in token
    body = token[2:]
    assert base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode() == url


def test_graph_access_needs_the_tenant_and_client_ids(monkeypatch):
    monkeypatch.setattr(env, "SPREADSHEET_TENANT_ID", None)
    with pytest.raises(st.SpreadsheetError, match="SPREADSHEET_TENANT_ID"):
        st.GraphWorkbook("https://example.invalid/book.xlsx")


class _Response:
    def __init__(self, payload, ok=True):
        self.payload, self.ok = payload, ok
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self.payload


def test_a_cached_token_is_reused_until_two_minutes_before_expiry(tmp_path, monkeypatch):
    auth = st.DeviceCodeAuth("tenant", "client")
    auth.cache_path = tmp_path / "token.json"
    auth.cache_path.write_text(json.dumps({"access_token": "cached", "refresh_token": "r",
                                           "expires_at": time.time() + 3600}))
    monkeypatch.setattr(st.requests, "post", lambda *a, **k: pytest.fail("no network expected"))
    assert auth.access_token() == "cached"


def test_an_expiring_token_is_refreshed_and_the_refresh_token_kept(tmp_path, monkeypatch):
    auth = st.DeviceCodeAuth("tenant", "client")
    auth.cache_path = tmp_path / "token.json"
    auth.cache_path.write_text(json.dumps({"access_token": "old", "refresh_token": "keep",
                                           "expires_at": time.time() + 60}))
    posts = []
    monkeypatch.setattr(st.requests, "post", lambda url, data=None, **kw: posts.append((url, data))
                        or _Response({"access_token": "fresh", "expires_in": 3600}))
    assert auth.access_token() == "fresh"
    assert posts[0][0] == "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"
    assert posts[0][1]["grant_type"] == "refresh_token"
    assert json.loads(auth.cache_path.read_text())["refresh_token"] == "keep"


def test_graph_calls_are_scoped_to_one_workbook_session(monkeypatch):
    monkeypatch.setattr(env, "SPREADSHEET_TENANT_ID", "t")
    monkeypatch.setattr(env, "SPREADSHEET_CLIENT_ID", "c")
    monkeypatch.setattr(st.DeviceCodeAuth, "access_token", lambda self: "tok")
    calls = []

    def fake(method, url, headers=None, json=None, **kwargs):
        calls.append((method, url.removeprefix(st.GRAPH_ROOT), headers.get("workbook-session-id"), json))
        if url.endswith("/driveItem"):
            return _Response({"id": "ITEM", "parentReference": {"driveId": "DRIVE"}})
        if url.endswith("/createSession"):
            return _Response({"id": "SESSION"})
        return _Response({"values": [["current"]]})

    monkeypatch.setattr(st.requests, "request", fake)
    book = st.GraphWorkbook("https://example.invalid/book.xlsx")
    assert book.get_cell("Control Page", "A160") == "current"
    method, path, session, _ = calls[-1]
    assert (method, session) == ("GET", "SESSION")
    assert path == "/drives/DRIVE/items/ITEM/workbook/worksheets/Control%20Page/range(address='A160')"
    assert calls[1][3] == {"persistChanges": True}


def test_every_spreadsheet_tool_is_a_gated_write():
    assert st.READ_TOOLS == []
    assert {t.tool_name for t in st.WRITE_TOOLS} == {
        "prepare_spreadsheet_approval_queue", "propose_spreadsheet_cell_update",
        "apply_approved_spreadsheet_updates"}
