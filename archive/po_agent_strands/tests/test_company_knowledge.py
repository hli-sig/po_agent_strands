"""The company-knowledge seam: a reserved place for retrieval, runnable today."""

from pmagent.tools import company_knowledge


def test_company_knowledge_is_empty_until_documents_are_added(tmp_path, monkeypatch):
    monkeypatch.setattr(company_knowledge, "_DOCS_DIR", str(tmp_path / "missing"))
    assert company_knowledge.retrieve_company_context("billing") == ""
    monkeypatch.setattr(company_knowledge, "_DOCS_DIR", str(tmp_path))
    assert company_knowledge.retrieve_company_context("billing") == ""


def test_company_knowledge_returns_md_and_txt_files_in_name_order_capped(tmp_path, monkeypatch):
    (tmp_path / "b.txt").write_text("second\n")
    (tmp_path / "a.md").write_text("  first  ")
    (tmp_path / "c.pdf").write_text("ignored")
    monkeypatch.setattr(company_knowledge, "_DOCS_DIR", str(tmp_path))
    assert company_knowledge.retrieve_company_context("x") == "### a.md\nfirst\n\n### b.txt\nsecond"
    assert company_knowledge.retrieve_company_context("x", max_chars=10) == "### a.md\nf"
