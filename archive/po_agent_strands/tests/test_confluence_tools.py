"""Unit tests for the Confluence reader.

No network: the pure functions run on plain ADF dicts, and the client is
exercised through an injected fake session — the same seam as
`tests/test_jira_tools.py`.

The cases here are drawn from the real EDP *Enterprise Data Model* page, which
is why they look so specific: 220 headings, 96 entity sections, and 85 of those
sections carrying their column list in a screenshot rather than in text.
"""

import json

import pytest

from pmagent.tools import confluence_tools as ct


PAGE_URL = (
    "https://sigmahealthcare.atlassian.net/wiki/spaces/EDP/pages/1622769666/"
    "Enterprise+Data+Model#DimEmployee"
)


def heading(level: int, text: str) -> dict:
    return {"type": "heading", "attrs": {"level": level},
            "content": [{"type": "text", "text": text}]}


def paragraph(text: str) -> dict:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def media(media_id: str) -> dict:
    return {"type": "mediaSingle",
            "content": [{"type": "media", "attrs": {"id": media_id, "type": "file"}}]}


# The real page's shape in miniature: entity at level 2, its detail under a
# level-3 'Platinum' heading, and that detail is an image.
DOC = {"type": "doc", "version": 1, "content": [
    heading(2, "DimDate"),
    paragraph("The date dimension."),
    heading(2, "DimEmployee"),
    paragraph("The employee dimension captures details of employees."),
    heading(3, "Platinum"),
    media("69586bfc-67a6-4261-bd4e-b517d7c8efc4"),
    heading(2, "DimParty"),
    paragraph("The party dimension."),
]}


# ---------------------------------------------------------------------------
# Resolving what the user pasted
# ---------------------------------------------------------------------------


def test_a_pasted_page_url_yields_its_id():
    assert ct.page_id_from_url(PAGE_URL) == "1622769666"


def test_a_bare_id_is_accepted():
    assert ct.page_id_from_url("1622769666") == "1622769666"


def test_the_legacy_viewpage_url_still_resolves():
    assert ct.page_id_from_url(
        "https://x.atlassian.net/wiki/pages/viewpage.action?pageId=1622769666"
    ) == "1622769666"


def test_a_short_link_is_refused_by_name_rather_than_guessed():
    # /wiki/x/AbCd carries no page id at all. Guessing here would read the
    # wrong page and look like it worked.
    with pytest.raises(ct.ConfluenceError) as excinfo:
        ct.page_id_from_url("https://x.atlassian.net/wiki/x/AbCdE")
    assert "short link" in str(excinfo.value)


def test_an_unparseable_reference_says_what_a_good_one_looks_like():
    with pytest.raises(ct.ConfluenceError) as excinfo:
        ct.page_id_from_url("the data model page")
    assert "/wiki/spaces/" in str(excinfo.value)


def test_the_url_anchor_becomes_the_section():
    # Pasting the link you were reading should read the heading you were on.
    assert ct.section_from_url(PAGE_URL) == "DimEmployee"


def test_a_url_without_an_anchor_has_no_section():
    assert ct.section_from_url("https://x.atlassian.net/wiki/spaces/EDP/pages/1/T") == ""


# ---------------------------------------------------------------------------
# Splitting a page into sections
# ---------------------------------------------------------------------------


def test_a_page_splits_at_every_heading():
    titles = [s["title"] for s in ct.split_sections(DOC)]
    assert titles == ["DimDate", "DimEmployee", "Platinum", "DimParty"]


def test_content_before_the_first_heading_is_not_dropped():
    doc = {"type": "doc", "content": [paragraph("intro"), heading(2, "A")]}
    sections = ct.split_sections(doc)
    assert sections[0]["title"] == ""
    assert sections[0]["nodes"] == [paragraph("intro")]


def test_a_section_read_includes_its_deeper_headings():
    # DimEmployee's real content lives under a level-3 'Platinum' heading. A
    # level-2 slice that stopped at the next heading would drop the answer.
    sections = ct.split_sections(DOC)
    index = next(i for i, s in enumerate(sections) if s["title"] == "DimEmployee")
    slice_ = ct.section_and_children(sections, index)
    assert [s["title"] for s in slice_] == ["DimEmployee", "Platinum"]


def test_a_section_read_stops_at_the_next_peer_heading():
    sections = ct.split_sections(DOC)
    index = next(i for i, s in enumerate(sections) if s["title"] == "DimEmployee")
    assert "DimParty" not in [s["title"] for s in ct.section_and_children(sections, index)]


# ---------------------------------------------------------------------------
# Matching a heading name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["DimEmployee", "dimemployee", "Dim Employee", "dim-employee"])
def test_heading_matching_ignores_case_and_punctuation(name):
    # A URL anchor strips spaces, so the match has to survive that round trip.
    match, ambiguous = ct.find_section(ct.split_sections(DOC), name)
    assert match["title"] == "DimEmployee"
    assert ambiguous == []


def test_a_near_miss_matches_nothing_rather_than_the_nearest_thing():
    # "Employee" is not "DimEmployee". Returning the wrong section of a data
    # model is worse than returning none, because it reads as an answer.
    match, ambiguous = ct.find_section(ct.split_sections(DOC), "Employee")
    assert match is None and ambiguous == []


def test_duplicate_headings_are_reported_as_ambiguous_not_picked():
    doc = {"type": "doc", "content": [
        heading(2, "Platinum"), paragraph("a"), heading(2, "Platinum"), paragraph("b"),
    ]}
    match, ambiguous = ct.find_section(ct.split_sections(doc), "Platinum")
    assert match is None
    assert ambiguous == ["Platinum", "Platinum"]


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def test_media_ids_are_found_at_any_depth():
    nested = {"type": "doc", "content": [
        {"type": "table", "content": [{"type": "tableRow", "content": [media("deep-id")]}]},
    ]}
    assert ct.media_ids(nested["content"]) == ["deep-id"]


def test_media_ids_are_deduplicated_and_ordered():
    nodes = [media("a"), media("b"), media("a")]
    assert ct.media_ids(nodes) == ["a", "b"]


def test_a_section_with_no_images_says_the_text_is_all_of_it():
    sections = ct.split_sections(DOC)
    rendered = ct.render_section({"title": "EDM", "id": "1"},
                                 sections[0]["nodes"], "DimDate", 0)
    assert "no images" in rendered


def test_a_section_with_images_says_the_detail_is_in_them():
    # The failure this prevents: 211 characters of prose read as the whole
    # entity definition, with the column list sitting unmentioned in a PNG.
    sections = ct.split_sections(DOC)
    index = next(i for i, s in enumerate(sections) if s["title"] == "DimEmployee")
    nodes = [n for part in ct.section_and_children(sections, index) for n in part["nodes"]]
    rendered = ct.render_section({"title": "EDM", "id": "1"}, nodes, "DimEmployee", 1)
    assert "1 image(s)" in rendered
    assert "only in the image" in rendered


def test_image_blocks_are_labelled_strands_image_content():
    blocks = ct.image_blocks([("dim.png", "image/png", b"\x89PNG-bytes")])
    assert "1 of 1" in blocks[0]["text"] and "dim.png" in blocks[0]["text"]
    assert blocks[1] == {"image": {"format": "png", "source": {"bytes": b"\x89PNG-bytes"}}}


def test_an_unsupported_image_format_becomes_a_note_not_a_silent_drop():
    blocks = ct.image_blocks([("d.svg", "image/svg+xml", b"<svg/>")])
    assert blocks == [{"text": "Confluence image 1 of 1: d.svg [not shown: image/svg+xml is not a supported image format]"}]


# ---------------------------------------------------------------------------
# The outline path — how a 40,000-character page is kept out of the prompt
# ---------------------------------------------------------------------------


def test_the_outline_lists_headings_with_their_image_counts():
    outline = ct.render_outline({"title": "EDM", "id": "1"}, ct.split_sections(DOC), 39853)
    assert "39853 characters" in outline
    assert "Too large to read whole" in outline
    assert "- DimEmployee" in outline
    assert "[1 image(s)]" in outline


def test_clipping_announces_itself():
    # Every cap needs a voice.
    clipped = ct.clip("x" * 100, 10, "section text")
    assert clipped.startswith("x" * 10)
    assert "showing 10 of 100" in clipped


def test_text_within_the_limit_is_untouched():
    assert ct.clip("short", 100, "section text") == "short"


# ---------------------------------------------------------------------------
# Client, through an injected fake transport
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload, status_code=200, content=b""):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.content = content
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload


class FakeSession:
    """Returns queued payloads in order and records what was asked for."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.gets = []

    def get(self, url, params=None, timeout=None):
        self.gets.append((url, params))
        return self._responses.pop(0)


def make_client(responses):
    client = ct.ConfluenceClient.__new__(ct.ConfluenceClient)  # skip credential checks
    client._base = "https://example.atlassian.net/wiki"
    client._session = FakeSession(responses)
    return client


def test_a_page_body_is_parsed_from_adf():
    client = make_client([FakeResponse(
        {"id": "1", "title": "EDM",
         "body": {"atlas_doc_format": {"value": json.dumps(DOC)}}}
    )])
    page = client.get_page("1")
    assert client.page_doc(page)["content"][0]["attrs"]["level"] == 2
    assert client._session.gets[0][1] == {"body-format": "atlas_doc_format"}


def test_a_legacy_page_without_adf_says_so_instead_of_reading_as_empty():
    client = make_client([FakeResponse({"id": "9", "title": "Old", "body": {}})])
    with pytest.raises(ct.ConfluenceError) as excinfo:
        client.page_doc(client.get_page("9"))
    assert "legacy editor" in str(excinfo.value)


def test_an_http_error_carries_the_status_and_body():
    client = make_client([FakeResponse({"message": "nope"}, status_code=404)])
    with pytest.raises(ct.ConfluenceError) as excinfo:
        client.get_page("1")
    assert "404" in str(excinfo.value)


def test_attachments_follow_the_cursor_to_the_end():
    # The real page has 261 attachments across two pages of results; stopping
    # at the first page would silently lose images.
    client = make_client([
        FakeResponse({"results": [{"fileId": "a"}], "_links": {"next": "/x?cursor=abc&limit=250"}}),
        FakeResponse({"results": [{"fileId": "b"}], "_links": {}}),
    ])
    assert [a["fileId"] for a in client.attachments("1")] == ["a", "b"]
    assert client._session.gets[1][1]["cursor"] == "abc"


def test_search_results_use_the_link_confluence_returned():
    # A guessed /pages/<id> path is not a route Confluence serves. Use the
    # webui link from the response, so the URL in the answer actually opens.
    rendered = ct.render_search_results(
        [{"title": "EDP CDS Extracts", "url": "/spaces/EDP/pages/936804360/EDP+CDS+Extracts",
          "excerpt": "<b>SAP</b> extractors", "content": {"id": "936804360", "title": "EDP CDS Extracts"}}],
        "extracts", "https://x.atlassian.net/wiki",
    )
    assert "https://x.atlassian.net/wiki/spaces/EDP/pages/936804360/EDP+CDS+Extracts" in rendered
    assert "SAP extractors" in rendered  # html stripped from the excerpt


def test_no_search_results_says_so_rather_than_returning_nothing():
    assert "No Confluence pages matched" in ct.render_search_results([], "zzz", "https://x")


def test_search_scopes_to_a_space_and_escapes_the_query():
    client = make_client([FakeResponse({"results": []})])
    client.search('data "model"', space="EDP", limit=5)
    cql = client._session.gets[0][1]["cql"]
    assert cql.startswith('space = "EDP"')
    assert '\\"model\\"' in cql


def test_images_are_capped_and_the_cap_is_announced():
    attachments = [
        {"fileId": f"id{i}", "mediaType": "image/png", "fileSize": 100,
         "title": f"i{i}.png", "_links": {"download": f"/d/{i}"}}
        for i in range(10)
    ]
    client = make_client(
        [FakeResponse({"results": attachments, "_links": {}})]
        + [FakeResponse(None, content=b"png") for _ in range(ct._MAX_IMAGES)]
    )
    images, notes = ct.collect_images(client, "1", [f"id{i}" for i in range(10)])
    assert len(images) == ct._MAX_IMAGES
    assert any(f"showing {ct._MAX_IMAGES} of 10" in note for note in notes)


def test_an_oversized_image_is_skipped_with_a_note():
    client = make_client([FakeResponse({"results": [
        {"fileId": "big", "mediaType": "image/png", "fileSize": ct._MAX_IMAGE_BYTES + 1,
         "title": "big.png", "_links": {"download": "/d/1"}}], "_links": {}})])
    images, notes = ct.collect_images(client, "1", ["big"])
    assert images == []
    assert any("too large" in note for note in notes)


def test_a_non_image_attachment_is_skipped_with_a_note():
    client = make_client([FakeResponse({"results": [
        {"fileId": "doc", "mediaType": "application/pdf", "fileSize": 10,
         "title": "spec.pdf", "_links": {"download": "/d/1"}}], "_links": {}})])
    images, notes = ct.collect_images(client, "1", ["doc"])
    assert images == []
    assert any("not an image" in note for note in notes)


def test_a_referenced_image_with_no_attachment_is_reported():
    # Silence here would mean an entity's columns just quietly not arriving.
    client = make_client([FakeResponse({"results": [], "_links": {}})])
    images, notes = ct.collect_images(client, "1", ["missing"])
    assert images == []
    assert any("no attachment" in note for note in notes)


# ---------------------------------------------------------------------------
# The tool itself — returns a Strands ToolResult with the images inside it
# ---------------------------------------------------------------------------


def _page_payload(doc=DOC):
    return {"id": "1622769666", "title": "Enterprise Data Model",
            "body": {"atlas_doc_format": {"value": json.dumps(doc)}}}


def _attachment(file_id="69586bfc-67a6-4261-bd4e-b517d7c8efc4"):
    return {"fileId": file_id, "mediaType": "image/png", "fileSize": 100,
            "title": "platinum.png", "_links": {"download": "/download/p.png"}}


def test_a_section_read_returns_text_then_labelled_images(monkeypatch):
    client = make_client([
        FakeResponse(_page_payload()),
        FakeResponse({"results": [_attachment()], "_links": {}}),
        FakeResponse(None, content=b"PNGDATA"),
    ])
    monkeypatch.setattr(ct, "get_client", lambda: client)

    result = ct.read_confluence_page(PAGE_URL)  # section comes from the #anchor

    assert result["status"] == "success"
    text, label, image = result["content"]
    assert "DimEmployee" in text["text"] and "in order, 1 to 1" in text["text"]
    assert "Confluence image 1 of 1: platinum.png" == label["text"]
    assert image == {"image": {"format": "png", "source": {"bytes": b"PNGDATA"}}}


def test_a_missing_section_is_a_text_only_answer_listing_the_real_ones(monkeypatch):
    client = make_client([FakeResponse(_page_payload())])
    monkeypatch.setattr(ct, "get_client", lambda: client)

    result = ct.read_confluence_page("1622769666", section="DimNope")

    assert result["status"] == "success"
    assert len(result["content"]) == 1
    assert "No section called 'DimNope'" in result["content"][0]["text"]
    assert "DimEmployee" in result["content"][0]["text"]


def test_a_failure_is_an_answer_not_a_traceback(monkeypatch):
    def boom():
        raise ct.ConfluenceError("403 forbidden")
    monkeypatch.setattr(ct, "get_client", boom)

    result = ct.read_confluence_page("1622769666", section="DimEmployee")
    assert result["content"] == [{"text": "Could not read Confluence page: 403 forbidden"}]


def test_the_tool_result_passes_through_the_strands_decorator_unwrapped():
    """A dict with status+content is taken as the ToolResult itself, not JSON-dumped."""
    from strands.tools.decorator import DecoratedFunctionTool

    assert isinstance(ct.read_confluence_page, DecoratedFunctionTool)
    wrapped = ct.read_confluence_page._wrap_tool_result("t1", {"status": "success", "content": [{"text": "x"}]})
    assert wrapped.tool_result == {"status": "success", "content": [{"text": "x"}], "toolUseId": "t1"}
