""" 
Central location for reading environment variables

load the `.env` file once, expose
the values as module-level constants, and validate that the *minimum* required
variables are present. Every other module imports from here instead of calling
`os.getenv` all over the codebase, so there is a single source of truth.

"""

from dotenv import load_dotenv
import os

#Load variables from .env files

load_dotenv()

#LLM configuration
# Which provider to use: "anthropic" (default) or "openai".
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()

# Default model per provider. Override with LLM_MODEL if you want something else.
# These defaults are intentionally the cheaper/faster tiers — good enough for an
# MVP and keeps your token bill low while you iterate.
_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-5.6-luna",
}
LLM_MODEL = os.getenv("LLM_MODEL", _DEFAULT_MODELS.get(LLM_PROVIDER, "claude-sonnet-5"))

# Provider API keys. Only the key for the *selected* provider is required.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# OpenAI only. How much internal reasoning the model does: "none" (default),
# "low", "medium" or "high". Anything above "none" forces the Responses API —
# see `pmagent/llm.py` for why.
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "none").strip().lower()

# OpenAI only, and only when LLM_REASONING_EFFORT is not "none": ask the
# Responses API for a reasoning *summary* ("auto", "concise" or "detailed") so the
# web UI can show it in the thinking chain. Unset (the default) sends nothing —
# OpenAI may require organisation verification for summaries. A summary is a
# condensed account of the model's reasoning, not the raw chain of thought.
LLM_REASONING_SUMMARY = os.getenv("LLM_REASONING_SUMMARY", "").strip().lower()

# Anthropic only: Strands' AnthropicModel requires an explicit output cap. A full
# PRD is long, and hitting the cap mid-answer raises MaxTokensReachedException,
# so the default is generous.
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "16000"))


#Jira Confirguration
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL")
# Atlassian account email, should be a service account later on
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "CSCI")
# Custom field id for story points. Sigma's "Story point estimate" field is
# customfield_10052 — override if your Jira instance uses a different id.
JIRA_STORY_POINTS_FIELD = os.getenv("JIRA_STORY_POINTS_FIELD", "customfield_10052").strip()
# Custom field id for the "Flagged" / impediment marker. Also instance-specific;
# customfield_10021 is the common Jira Cloud default.
JIRA_FLAGGED_FIELD = os.getenv("JIRA_FLAGGED_FIELD", "customfield_10021").strip()
# Custom field id used to put an issue into a sprint at creation time.
# customfield_10020 is the common Jira Cloud default.
JIRA_SPRINT_FIELD = os.getenv("JIRA_SPRINT_FIELD", "customfield_10020").strip()
# Days an in-progress issue can sit untouched before sprint reporting calls it
# stuck. Tuned for a two-week sprint; only applies to work already in flight.
JIRA_STUCK_THRESHOLD_DAYS = int(os.getenv("JIRA_STUCK_THRESHOLD_DAYS", "10"))
# How a user-visible sprint number maps to the sprint's real name in Jira.
# For CSCI, "sprint 31" means the sprint named "Supply Chain Sprint 31".
JIRA_SPRINT_NAME_TEMPLATE = os.getenv(
    "JIRA_SPRINT_NAME_TEMPLATE", "Supply Chain Sprint {n}"
)


# Confluence Cloud. Same Atlassian site and the SAME credentials as Jira — an
# Atlassian API token authenticates against every product on the tenant — so
# there is no separate email/token here on purpose. Confluence Cloud lives under
# /wiki on the same host.
CONFLUENCE_BASE_URL = os.getenv(
    "CONFLUENCE_BASE_URL", f"{(JIRA_BASE_URL or '').rstrip('/')}/wiki"
)
# Default space for searches when the user doesn't name one. EDP is the
# Enterprise Data Platform space.
CONFLUENCE_SPACE_KEY = os.getenv("CONFLUENCE_SPACE_KEY", "EDP")


# MCP (Model Context Protocol) servers.
# Lucid's hosted MCP server (diagram search/create) — see
# pmagent/tools/mcp_tools.py for the client that uses these, and
# ../snowflake/sql/lucid_mcp_setup.sql for the equivalent Snowflake-side setup.
# Unset LUCID_MCP_AUTH_TOKEN is fine if your Lucid plan relies on Dynamic
# Client Registration (per-user OAuth) rather than a static client secret.
# Off by default: the diagram lane then has only its local brief-drafting tool.
LUCID_MCP_ENABLED = os.getenv("LUCID_MCP_ENABLED", "false").strip().lower() in ("1", "true", "yes")
LUCID_MCP_URL = os.getenv("LUCID_MCP_URL", "https://mcp.lucid.app/mcp")
LUCID_MCP_AUTH_TOKEN = os.getenv("LUCID_MCP_AUTH_TOKEN")


# Microsoft Graph delegated access for the approval-gated spreadsheet tool.
# A public-client app registration is required; no client secret is used.
SPREADSHEET_TENANT_ID = os.getenv("SPREADSHEET_TENANT_ID")
SPREADSHEET_CLIENT_ID = os.getenv("SPREADSHEET_CLIENT_ID")


# Web server (web.py). When set, every /api/v1 route except /health requires
# `Authorization: Bearer <token>`. Unset is fine for a loopback-only local tool.
PMAGENT_API_TOKEN = os.getenv("PMAGENT_API_TOKEN") or None


# FY budget converter (pmagent/tools/fy_budget + tools/finance_tools.py).
# Approved source workbooks in, generated Snowflake-ingest CSV + audit metadata
# out. Both default under data/fy_budget/, which .gitignore already excludes —
# these are real financial figures and must never be committed.
FY_BUDGET_INPUT_DIR = os.getenv("FY_BUDGET_INPUT_DIR", "data/fy_budget/input")
FY_BUDGET_OUTPUT_DIR = os.getenv("FY_BUDGET_OUTPUT_DIR", "data/fy_budget/output")
# Where the FY regression fixtures live, if not in the package's own fixtures/
# folder. Unset is normal: tests/test_fy_budget_conversion.py skips itself
# rather than failing a fresh clone that has no real budget data.
FY_FIXTURES_DIR = os.getenv("FY_FIXTURES_DIR")


def validate() -> None:
    """Fail fast if the minimum config for the selected provider is missing.

    """
    if LLM_PROVIDER == "anthropic" and not ANTHROPIC_API_KEY:
        raise ValueError(
            "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set. "
            "Add it to .env file."
        )
    if LLM_PROVIDER == "openai" and not OPENAI_API_KEY:
        raise ValueError(
            "LLM_PROVIDER=openai but OPENAI_API_KEY is not set. "
            "Add it to  .env file."
        )


def validate_jira() -> None:
    """Fail fast if Jira credentials are missing.

    Called by `JiraClient.__init__`, not at import time, so modules that only
    need the pure modules in `pmagent/tools/jira/` still import cleanly.
    """
    missing = [
        name
        for name, value in (
            ("JIRA_BASE_URL", JIRA_BASE_URL),
            ("JIRA_EMAIL", JIRA_EMAIL),
            ("JIRA_API_TOKEN", JIRA_API_TOKEN),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            f"Jira is not configured: {', '.join(missing)} not set. "
            "Add them to your .env file — there is no offline mode."
        )
