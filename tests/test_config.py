from pathlib import Path
import pytest
import pmagent.env as e

def test_default_value_exist():
    assert e.LLM_PROVIDER != ''


def test_validate_missing_anthropic_key(monkeypatch):
    """Test that validate() raises a ValueError when Anthropic is selected without an API key."""
    # Set the module-level variables for the test
    monkeypatch.setattr(e, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(e, "ANTHROPIC_API_KEY", "")

    # Assert that ValueError is raised with the correct message
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is not set"):
        e.validate()