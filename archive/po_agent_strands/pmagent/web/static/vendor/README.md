# Vendored browser libraries

Served from `/static/vendor/` so the page loads no third-party script (see the
Content-Security-Policy in `pmagent/web/app.py`) and works offline.

| File | Library | Version | Licence | Source | SHA-256 |
|------|---------|---------|---------|--------|---------|
| `marked.min.js` | marked (Markdown → HTML) | 15.0.12 | MIT | https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js | `3e7e7d7feb3e5d58cb6c804f68ab5c24cc7e5eb6270fd6e5cbb9124739217d0c` |
| `purify.min.js` | DOMPurify (HTML sanitiser) | 3.2.6 | Apache-2.0 / MPL-2.0 | https://cdn.jsdelivr.net/npm/dompurify@3.2.6/dist/purify.min.js | `89e1fa7647cb495370d3a997ace4387f5d15d9f4c5af12352c53daa400956287` |

To upgrade: download the new pinned file, update this table, and run
`uv run pytest tests/test_web_api.py` (it checks these hashes).
