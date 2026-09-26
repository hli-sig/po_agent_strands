"""The published course page is generated from the Markdown lessons — never edited by hand."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_course_html_is_up_to_date():
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_course.py"), "--check"],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_course_page_follows_the_artifact_contract():
    page = (ROOT / "docs" / "learning" / "course.html").read_text(encoding="utf-8")
    assert page.startswith("<title>")
    for tag in ("<!doctype", "<html", "<head>", "<body"):
        assert tag not in page.lower()
    assert ':root[data-theme="dark"]' in page and "prefers-color-scheme: dark" in page
    assert "<script" not in page                     # nothing to load, nothing to run
    for lesson in range(1, 18):
        assert f'id="lesson-{lesson:02d}"' in page
    assert 'id="rebuild"' in page and 'id="rebuild-domain"' in page and 'class="part"' in page
    assert 'id="api"' in page and "/api/v1/conversations/{conversation_id}/turns" in page
