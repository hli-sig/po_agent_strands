"""Approval-gated Microsoft Graph operations for project-control spreadsheets.

The tool supports any workbook URL the signed-in user can access. It writes
control data only after a human changes a queue row to ``Approved`` inside the
workbook. OAuth is delegated to the current user's Microsoft account; no
browser automation or client secret is used.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
from uuid import uuid4

import requests
from strands import tool

from pmagent import env


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
QUEUE_SHEET = "PM Agent Approvals"
QUEUE_TABLE = "PMAgentApprovals"
QUEUE_HEADERS = [
    "RequestId", "RequestedAt", "Operation", "Target", "ExpectedValue",
    "ProposedValue", "Status", "ApprovedBy", "AppliedAt", "Result",
]
_SCOPES = "https://graph.microsoft.com/Files.ReadWrite User.Read offline_access"


class SpreadsheetError(RuntimeError):
    """Raised when authentication or Microsoft Graph rejects an operation."""


class ApprovalWorkbook(Protocol):
    def get_cell(self, sheet: str, cell: str) -> str: ...

    def set_cell(self, sheet: str, cell: str, value: str) -> None: ...

    def append_request(self, row: list[str]) -> None: ...

    def requests(self) -> list[tuple[int, list[str]]]: ...

    def replace_request(self, index: int, row: list[str]) -> None: ...


def propose_cell_update(workbook: ApprovalWorkbook, sheet: str, cell: str, value: str) -> str:
    """Capture the current value and append a human-reviewable request."""
    request_id = f"scr_{uuid4().hex}"
    workbook.append_request([
        request_id,
        datetime.now(UTC).isoformat(),
        "set_cell",
        f"{sheet}!{cell}",
        workbook.get_cell(sheet, cell),
        value,
        "Pending",
        "",
        "",
        "",
    ])
    return request_id


def apply_approved_requests(workbook: ApprovalWorkbook) -> list[str]:
    """Apply queue rows that are approved and still match their original value."""
    applied = []
    for index, row in workbook.requests():
        if row[2] != "set_cell" or row[6] != "Approved":
            continue
        sheet, cell = row[3].rsplit("!", 1)
        if workbook.get_cell(sheet, cell) != row[4]:
            row[6], row[8], row[9] = "Conflicted", "", "Target changed after proposal"
        else:
            workbook.set_cell(sheet, cell, row[5])
            row[6], row[8], row[9] = "Applied", datetime.now(UTC).isoformat(), "Applied"
            applied.append(row[0])
        workbook.replace_request(index, row)
    return applied


class DeviceCodeAuth:
    """Small delegated OAuth client with a per-user local token cache."""

    def __init__(self, tenant_id: str, client_id: str):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.cache_path = Path.home() / ".pmagent" / "microsoft-graph-token.json"

    @property
    def token_url(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"

    def access_token(self) -> str:
        cached = self._cache()
        if cached and cached["expires_at"] > time.time() + 120:
            return cached["access_token"]
        if cached and cached.get("refresh_token"):
            return self._save(self._post(self.token_url, {
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": cached["refresh_token"],
                "scope": _SCOPES,
            }))
        return self._device_login()

    def _device_login(self) -> str:
        device = self._post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/devicecode",
            {"client_id": self.client_id, "scope": _SCOPES},
        )
        print(device["message"])
        deadline = time.time() + device["expires_in"]
        interval = device.get("interval", 5)
        while time.time() < deadline:
            time.sleep(interval)
            try:
                return self._save(self._post(self.token_url, {
                    "client_id": self.client_id,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device["device_code"],
                }))
            except SpreadsheetError as error:
                if "authorization_pending" in str(error):
                    continue
                if "slow_down" in str(error):
                    interval += 5
                    continue
                raise
        raise SpreadsheetError("Microsoft device-code sign-in timed out")

    def _cache(self) -> dict | None:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def _save(self, response: dict) -> str:
        if "access_token" not in response:
            raise SpreadsheetError(response.get("error_description", "Token request failed"))
        existing = self._cache() or {}
        record = {
            "access_token": response["access_token"],
            "refresh_token": response.get("refresh_token", existing.get("refresh_token")),
            "expires_at": time.time() + int(response["expires_in"]),
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(record), encoding="utf-8")
        return record["access_token"]

    @staticmethod
    def _post(url: str, data: dict[str, str]) -> dict:
        response = requests.post(url, data=data, timeout=30)
        if response.ok:
            return response.json()
        raise SpreadsheetError(response.text)


class GraphWorkbook:
    """The narrow Microsoft Graph surface needed by the approval workflow."""

    def __init__(self, workbook_url: str):
        if not env.SPREADSHEET_TENANT_ID or not env.SPREADSHEET_CLIENT_ID:
            raise SpreadsheetError(
                "Set SPREADSHEET_TENANT_ID and SPREADSHEET_CLIENT_ID in .env first."
            )
        self.auth = DeviceCodeAuth(env.SPREADSHEET_TENANT_ID, env.SPREADSHEET_CLIENT_ID)
        item = self._request("GET", f"/shares/{_sharing_token(workbook_url)}/driveItem")
        self.drive_id = item["parentReference"]["driveId"]
        self.item_id = item["id"]
        self.session_id = self._request(
            "POST", f"{self._base}/createSession", {"persistChanges": True}
        )["id"]

    @property
    def _base(self) -> str:
        return f"/drives/{self.drive_id}/items/{self.item_id}/workbook"

    def get_cell(self, sheet: str, cell: str) -> str:
        values = self._request("GET", self._range_path(sheet, cell))["values"]
        return "" if values[0][0] is None else str(values[0][0])

    def set_cell(self, sheet: str, cell: str, value: str) -> None:
        self._request("PATCH", self._range_path(sheet, cell), {"values": [[value]]})

    def bootstrap_queue(self) -> None:
        self._request("POST", f"{self._base}/worksheets/add", {"name": QUEUE_SHEET})
        self._request("PATCH", self._range_path(QUEUE_SHEET, "A1:J1"), {"values": [QUEUE_HEADERS]})
        table = self._request(
            "POST", f"{self._base}/tables/add",
            {"address": f"{QUEUE_SHEET}!A1:J1", "hasHeaders": True},
        )
        self._request("PATCH", f"{self._base}/tables/{quote(table['id'], safe='')}", {"name": QUEUE_TABLE})

    def append_request(self, row: list[str]) -> None:
        self._request("POST", f"{self._base}/tables/{QUEUE_TABLE}/rows/add", {"index": None, "values": [row]})

    def requests(self) -> list[tuple[int, list[str]]]:
        payload = self._request("GET", f"{self._base}/tables/{QUEUE_TABLE}/rows")
        return [
            (item["index"], ["" if value is None else str(value) for value in item["values"][0]])
            for item in payload.get("value", [])
        ]

    def replace_request(self, index: int, row: list[str]) -> None:
        self._request(
            "PATCH", f"{self._base}/tables/{QUEUE_TABLE}/rows/itemAt(index={index})/range",
            {"values": [row]},
        )

    def _range_path(self, sheet: str, address: str) -> str:
        return f"{self._base}/worksheets/{quote(sheet, safe='')}/range(address='{quote(address, safe=':')}')"

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.auth.access_token()}"}
        if hasattr(self, "session_id"):
            headers["workbook-session-id"] = self.session_id
        response = requests.request(method, GRAPH_ROOT + path, headers=headers, json=payload, timeout=30)
        if response.ok:
            return response.json() if response.content else {}
        raise SpreadsheetError(response.text)


def _sharing_token(workbook_url: str) -> str:
    return "u!" + base64.urlsafe_b64encode(workbook_url.encode()).decode().rstrip("=")


@tool
def prepare_spreadsheet_approval_queue(workbook_url: str) -> str:
    """Create the PM Agent Approvals sheet in a workbook.

    Call only after the user explicitly approves this setup action. It creates
    a worksheet and table; it does not alter project-control data itself.
    """
    GraphWorkbook(workbook_url).bootstrap_queue()
    return "Created the PM Agent Approvals sheet and queue table."


@tool
def propose_spreadsheet_cell_update(
    workbook_url: str, sheet: str, cell: str, value: str
) -> str:
    """Create a pending, human-reviewable update to one spreadsheet cell.

    This does not update the target cell. A human must set the queue row's
    Status to Approved inside the workbook before it can be applied.
    """
    request_id = propose_cell_update(GraphWorkbook(workbook_url), sheet, cell, value)
    return f"Created pending spreadsheet request {request_id} for {sheet}!{cell}."


@tool
def apply_approved_spreadsheet_updates(workbook_url: str) -> str:
    """Apply human-approved PM Agent Approvals rows in a workbook.

    Call only after the user confirms that the relevant request is Approved in
    the spreadsheet. Conflicting target values are not overwritten.
    """
    applied = apply_approved_requests(GraphWorkbook(workbook_url))
    return "Applied: " + ", ".join(applied) if applied else "No approved spreadsheet updates to apply."


# Every tool module declares both lists, even when one is empty — `tests/
# test_agent_lanes.py` reconciles them against `gate.WRITE_TOOL_NAMES` and
# fails on any tool defined here that appears in neither. An empty READ_TOOLS
# is a statement ("nothing here is safe to run unattended"), not an oversight.
#
# All three are writes. `prepare_spreadsheet_approval_queue` creates the
# approval sheet inside the user's real workbook, and `propose_...` writes a
# queue row — neither is a preview just because the *target* cell is untouched.
READ_TOOLS = []
WRITE_TOOLS = [
    prepare_spreadsheet_approval_queue,
    propose_spreadsheet_cell_update,
    apply_approved_spreadsheet_updates,
]
