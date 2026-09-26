"""JQL construction.

Everything that queries Jira builds its query here, so quoting and escaping
live in exactly one place and nothing hand-concatenates JQL.
"""

from __future__ import annotations


def _quote(value: str) -> str:
    """Quote a JQL string literal, escaping embedded quotes and backslashes."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class JqlBuilder:
    """Compose a JQL query from typed parts.

    Everything that queries Jira goes through here so quoting/escaping lives in
    one place and sprint reporting is just "the same search with one more
    clause" rather than a separate Agile-API code path.
    """

    def __init__(self) -> None:
        self._clauses: list[str] = []
        self._order_by = "created DESC"

    def project(self, key: str) -> JqlBuilder:
        self._clauses.append(f"project = {_quote(key)}")
        return self

    def sprint(self, sprint_id: int) -> JqlBuilder:
        # Numeric internal sprint id — never the user-visible number. Resolve
        # a visible number with `JiraClient.resolve_sprint` first.
        self._clauses.append(f"sprint = {int(sprint_id)}")
        return self

    def status_category(self, category: str, negate: bool = False) -> JqlBuilder:
        op = "!=" if negate else "="
        self._clauses.append(f"statusCategory {op} {_quote(category)}")
        return self

    def issue_type(self, *types: str) -> JqlBuilder:
        if types:
            joined = ", ".join(_quote(t) for t in types)
            self._clauses.append(f"issuetype IN ({joined})")
        return self

    def assignee(self, account: str) -> JqlBuilder:
        self._clauses.append(f"assignee = {_quote(account)}")
        return self

    def text(self, term: str) -> JqlBuilder:
        self._clauses.append(f"text ~ {_quote(term)}")
        return self

    def keys(self, *issue_keys: str) -> JqlBuilder:
        """Match an exact set of issue keys.

        The precise counterpart to `text`/`summary` matching. When the caller
        already knows which issues it wants, a `~` search is the wrong tool: it
        silently returns neighbours whose summaries happen to look alike, and a
        set that grew by one is indistinguishable from the set that was asked
        for.
        """
        cleaned = [k.strip() for k in issue_keys if k and k.strip()]
        if cleaned:
            joined = ", ".join(_quote(k) for k in cleaned)
            self._clauses.append(f"key IN ({joined})")
        return self

    def raw(self, clause: str) -> JqlBuilder:
        """Append an already-formed JQL fragment verbatim."""
        if clause.strip():
            self._clauses.append(f"({clause.strip()})")
        return self

    def order_by(self, expression: str) -> JqlBuilder:
        self._order_by = expression
        return self

    def build(self) -> str:
        if not self._clauses:
            raise ValueError("JqlBuilder needs at least one clause.")
        where = " AND ".join(self._clauses)
        return f"{where} ORDER BY {self._order_by}"
