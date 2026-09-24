"""
Company-knowledge retrieval — the *reserved seam* for grounding PRDs from existing
documentation (Confluence, past PRDs, ADRs, etc.).

v1 behaviour: this returns nothing useful unless you drop sample
files into `sample_data/company_docs/`. That keeps the seam **runnable today**
(you can test grounding by adding a markdown file) without building a vector
store before it's needed.

To make it production-grade later, replace the body of `retrieve_company_context`
with a real retriever:
  - Confluence: query the Confluence Cloud REST API for pages in the relevant
    space, then chunk + embed them.
  - RAG: embed the `topic`, search a vector store (pgvector/Pinecone/etc.), and
    return the top-k chunks.
The function signature and call site (the Writer node) stay the same, so nothing
else has to change.
"""

import glob
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DOCS_DIR = os.path.join(_PROJECT_ROOT, "sample_data", "company_docs")



def retrieve_company_context(topic: str, max_chars: int = 4000) -> str:
    """Return company documentation relevant to `topic`, or an empty string.

    Args:
        topic: A short description of what the PRD is about (used for relevance
            once a real retriever is wired in).
        max_chars: Cap on returned context so we don't blow the prompt budget.

    Returns:
        Concatenated relevant context, or "" if no knowledge base is connected.

    Current implementation: reads any `.md`/`.txt` files in
    `sample_data/company_docs/` and returns them (truncated). This is a stand-in
    for real retrieval — see module docstring for how to replace it.
    """
    if not os.path.isdir(_DOCS_DIR):
        return ""
    files = sorted(glob.glob(os.path.join(_DOCS_DIR, "*.md"))
                   + glob.glob(os.path.join(_DOCS_DIR, "*.txt")))
    if not files:
        return ""
    chunks: list[str] = []

    for path in files:
        with open(path, "r") as f:
            chunks.append(f"### {os.path.basename(path)}\n{f.read().strip()}")

    context = "\n\n".join(chunks)
    return context[:max_chars]
