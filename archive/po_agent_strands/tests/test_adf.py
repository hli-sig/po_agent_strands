"""Unit tests for the Markdown -> ADF renderer. Pure functions, no I/O."""

from pmagent.tools.adf import inline_nodes, render_description, to_adf, to_text


def types_of(doc):
    return [node["type"] for node in doc["content"]]


def text_of(node):
    """Concatenate all text in a node subtree."""
    if node.get("type") == "text":
        return node["text"]
    return "".join(text_of(child) for child in node.get("content", []))


# ---------------------------------------------------------------------------
# Document structure
# ---------------------------------------------------------------------------
def test_empty_input_still_produces_a_valid_document():
    doc = to_adf("")
    assert doc["type"] == "doc"
    assert doc["version"] == 1
    # ADF rejects an empty content array.
    assert doc["content"]


def test_blank_lines_separate_paragraphs():
    doc = to_adf("First para.\n\nSecond para.")
    assert types_of(doc) == ["paragraph", "paragraph"]
    assert text_of(doc["content"][1]) == "Second para."


def test_consecutive_lines_join_into_one_paragraph():
    doc = to_adf("one\ntwo")
    assert types_of(doc) == ["paragraph"]
    assert text_of(doc["content"][0]) == "one two"


def test_headings_carry_their_level():
    doc = to_adf("# One\n### Three")
    assert types_of(doc) == ["heading", "heading"]
    assert doc["content"][0]["attrs"]["level"] == 1
    assert doc["content"][1]["attrs"]["level"] == 3


def test_bullet_list_becomes_one_list_with_items():
    doc = to_adf("- alpha\n- beta")
    assert types_of(doc) == ["bulletList"]
    items = doc["content"][0]["content"]
    assert len(items) == 2
    assert text_of(items[1]) == "beta"


def test_ordered_list_is_distinct_from_bullets():
    doc = to_adf("1. first\n2. second")
    assert types_of(doc) == ["orderedList"]
    assert doc["content"][0]["attrs"]["order"] == 1


def test_switching_list_kind_starts_a_new_list():
    assert types_of(to_adf("- a\n1. b")) == ["bulletList", "orderedList"]


def test_paragraph_then_list_are_separate_nodes():
    assert types_of(to_adf("Intro:\n- a\n- b")) == ["paragraph", "bulletList"]


def test_code_block_keeps_its_language_and_raw_newlines():
    doc = to_adf("```sql\nSELECT 1\nFROM t\n```")
    assert types_of(doc) == ["codeBlock"]
    assert doc["content"][0]["attrs"]["language"] == "sql"
    assert doc["content"][0]["content"][0]["text"] == "SELECT 1\nFROM t"


def test_markdown_inside_a_code_block_is_not_parsed():
    doc = to_adf("```\n# not a heading\n- not a bullet\n```")
    assert types_of(doc) == ["codeBlock"]


def test_unterminated_code_fence_still_yields_a_valid_document():
    doc = to_adf("```\nunclosed")
    assert types_of(doc) == ["codeBlock"]


# ---------------------------------------------------------------------------
# Inline marks
# ---------------------------------------------------------------------------
def test_bold_becomes_a_strong_mark():
    nodes = inline_nodes("plain **loud** plain")
    assert nodes[1]["text"] == "loud"
    assert nodes[1]["marks"] == [{"type": "strong"}]


def test_inline_code_becomes_a_code_mark():
    nodes = inline_nodes("call `foo()` now")
    assert nodes[1]["text"] == "foo()"
    assert nodes[1]["marks"] == [{"type": "code"}]


def test_no_empty_text_nodes_are_emitted():
    # ADF rejects text nodes with empty strings.
    for node in inline_nodes("**bold**"):
        assert node["text"]


def test_plain_text_needs_no_marks():
    assert "marks" not in inline_nodes("just words")[0]


# ---------------------------------------------------------------------------
# render_description — the acceptance-criteria fix
# ---------------------------------------------------------------------------
def test_acceptance_criteria_render_as_a_heading_plus_bullet_list():
    doc = render_description("Body text.", ["Given A, then B", "Given C, then D"])

    assert types_of(doc) == ["paragraph", "heading", "bulletList"]
    assert text_of(doc["content"][1]) == "Acceptance Criteria"

    items = doc["content"][2]["content"]
    assert [text_of(i) for i in items] == ["Given A, then B", "Given C, then D"]


def test_no_acceptance_criteria_means_no_heading():
    doc = render_description("Body only.", [])
    assert types_of(doc) == ["paragraph"]


def test_multi_section_description_keeps_its_structure():
    doc = render_description(
        "As a user, I want X.\n\n## Context\nWhy.\n\n## Scope\n- one\n- two",
        ["It works"],
    )
    assert types_of(doc) == [
        "paragraph",
        "heading",
        "paragraph",
        "heading",
        "bulletList",
        "heading",
        "bulletList",
    ]


# ---------------------------------------------------------------------------
# ADF -> text (the read direction)
# ---------------------------------------------------------------------------
def test_text_survives_a_round_trip_through_adf():
    source = "# Context\n\nBlocked on StockDB.\n\n- waiting on extract\n- mapping unconfirmed"
    assert to_text(to_adf(source)).strip() == source.strip()


def test_mentions_render_as_names_not_account_ids():
    # A comment saying "pending review from @Alan Yuen" is useless if it comes
    # back as an opaque account id.
    doc = {"type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "pending review from "},
        {"type": "mention", "attrs": {"id": "557058:abc", "text": "@Alan Yuen"}},
    ]}]}
    assert to_text(doc) == "pending review from @Alan Yuen"


def test_a_mention_without_the_at_prefix_still_gets_one():
    doc = {"type": "mention", "attrs": {"id": "x", "text": "Alan Yuen"}}
    assert to_text(doc) == "@Alan Yuen"


def test_unknown_node_types_degrade_to_their_text():
    # A future Jira node type must lose its formatting, never its content.
    doc = {"type": "doc", "content": [
        {"type": "someFutureThing", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "still readable"}]}
        ]}
    ]}
    assert "still readable" in to_text(doc)


def test_nested_structures_keep_their_shape():
    doc = to_adf("1. first\n2. second")
    assert to_text(doc) == "1. first\n2. second"


def test_code_blocks_keep_their_fence_and_language():
    out = to_text(to_adf("```sql\nselect 1\n```"))
    assert out == "```sql\nselect 1\n```"


def test_none_and_junk_render_as_empty_string():
    # Jira returns null descriptions constantly; this must not raise.
    assert to_text(None) == ""
    assert to_text("not a node") == ""
    assert to_text({}) == ""


def test_table_rows_stay_on_consecutive_lines():
    # Double-spacing rows turns a small table into a wall and stops it reading
    # as tabular at all.
    doc = {"type": "doc", "content": [{"type": "table", "content": [
        {"type": "tableRow", "content": [
            {"type": "tableHeader", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Field"}]}]},
            {"type": "tableHeader", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Value"}]}]},
        ]},
        {"type": "tableRow", "content": [
            {"type": "tableCell", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Grain"}]}]},
            {"type": "tableCell", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "one row per sale"}]}]},
        ]},
    ]}]}
    assert to_text(doc) == "| Field | Value |\n| Grain | one row per sale |"
