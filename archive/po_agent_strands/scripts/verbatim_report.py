"""Compare every file of the original pmagent/ with this repo's (CRLF-insensitive).

    uv run scripts/verbatim_report.py [/path/to/PO_Agent]

Evidence for "the domain code was ported verbatim": lists each file as
IDENTICAL, CHANGED (with its diff line count), REMOVED, or NEW.
"""

import difflib
import sys
from pathlib import Path

ORIGINAL = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/hl2/development/PO_Agent")
HERE = Path(__file__).resolve().parent.parent


def files(root: Path) -> set[str]:
    return {
        str(p.relative_to(root))
        for p in (root / "pmagent").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".md")
    }


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").splitlines()


old, new = files(ORIGINAL), files(HERE)
rows = []
for name in sorted(old | new):
    if name not in new:
        rows.append(("REMOVED", name, ""))
    elif name not in old:
        rows.append(("NEW", name, ""))
    else:
        a, b = lines(ORIGINAL / name), lines(HERE / name)
        if a == b:
            rows.append(("IDENTICAL", name, ""))
            continue
        changed = [l for l in difflib.unified_diff(a, b, lineterm="", n=0)
                   if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        rows.append(("CHANGED", name, f"{len(changed)} +/- lines"))

width = max(len(r[1]) for r in rows)
for status, name, note in rows:
    print(f"{status:<10} {name:<{width}}  {note}")
counts = {s: sum(1 for r in rows if r[0] == s) for s in ("IDENTICAL", "CHANGED", "NEW", "REMOVED")}
print("\n" + ", ".join(f"{k}: {v}" for k, v in counts.items()))
