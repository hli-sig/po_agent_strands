"""The docs cite code by `path::Symbol`; make sure every citation still resolves.

Lessons are only useful while they point at real code. Citations use a symbol, not
a line number (`pmagent/gate.py::ApprovalGate.check_batch`), so ordinary edits
don't break them — but a rename or a deleted function does, and this test says so.
Also checks that every backticked repo path (`pmagent/...py`, `docs/...`) exists.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = sorted(
    [*ROOT.glob("docs/learning/*.md"), ROOT / "README.md", ROOT / "CLAUDE.md", ROOT / "docs/EVIDENCE.md"]
)

_CITATION = re.compile(r"`([\w./-]+\.py)::([\w.]+)`")
_PATH = re.compile(r"`((?:pmagent|tests|scripts|docs|main\.py)[\w./-]*)`")


def _symbols(path: Path) -> set[str]:
    """Top-level names and Class.method names defined in a Python file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        names.add(f"{node.name}.{item.name}")
                    elif isinstance(item, (ast.AnnAssign, ast.Assign)):
                        targets = [item.target] if isinstance(item, ast.AnnAssign) else item.targets
                        names |= {f"{node.name}.{t.id}" for t in targets if isinstance(t, ast.Name)}
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return names


def _citations():
    for doc in DOCS:
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8")
        for path, symbol in _CITATION.findall(text):
            yield pytest.param(doc, path, symbol, id=f"{doc.name}:{path}::{symbol}")


def _paths():
    for doc in DOCS:
        if not doc.exists():
            continue
        for path in set(_PATH.findall(doc.read_text(encoding="utf-8"))):
            if "*" in path or "<" in path:
                continue
            yield pytest.param(doc, path.rstrip("/.,"), id=f"{doc.name}:{path}")


def test_the_docs_exist():
    assert all(d.exists() for d in DOCS), [str(d) for d in DOCS if not d.exists()]


@pytest.mark.parametrize("doc,path,symbol", list(_citations()))
def test_every_cited_symbol_exists(doc, path, symbol):
    file = ROOT / path
    assert file.exists(), f"{doc.name} cites {path}, which does not exist"
    assert symbol in _symbols(file), f"{doc.name} cites {path}::{symbol}, which is not defined there"


@pytest.mark.parametrize("doc,path", list(_paths()))
def test_every_cited_path_exists(doc, path):
    assert (ROOT / path).exists(), f"{doc.name} mentions `{path}`, which does not exist"


def test_citations_were_found():
    # Guards the regex: a pattern that silently matched nothing would pass vacuously.
    assert len(list(_citations())) > 40
