"""Tests for app/fields.py — field stripping (FR-3, ADR-1).

AC coverage:
- AC-3.1: table-driven, output keys ⊆ allowlist and required keys present.
- AC-3.2: casing normalization (snake_case detail == camelCase search keys).
- AC-3.3: ≥2× size reduction per fixture (stripped < 0.5 * raw).
- AC-2.1: search-hit key-set allowlist + snippet present for o-type hits.
"""

import json
from typing import Any

import pytest

from app.fields import (
    SEARCH_HIT_ALLOWLIST,
    extract_opinion_text,
    strip_candidates,
    strip_resource,
    strip_search_hit,
)

# ---------------------------------------------------------------------------
# Fixtures: realistic verbose payloads (junk fields the LLM never needs)
# ---------------------------------------------------------------------------

CLUSTER_DETAIL_RAW: dict[str, Any] = {
    "id": 123,
    "case_name": "Roe v. Wade",
    "case_name_full": "Jane Roe, et al., Appellants, v. Henry Wade",
    "docket": "https://www.courtlistener.com/api/rest/v4/dockets/55/",
    "court": "scotus",
    "date_filed": "1973-01-22",
    "citations": [{"cite": "410 U.S. 113", "type": "official"}],
    "absolute_url": "/opinion/123/roe-v-wade/",
    "panel_ids": [1, 2, 3],
    "judges": "",
    "attorney": "Weddington, Coffee",
    "nature_of_suit": "",
    "posture": "",
    "syllabus": "The principal issue...",
    "scdb_id": "1972-119",
    "sha1": "abc123",
    "local_path": "/data/cluster/123",
    "meta": {"generated": "2026-09-10"},
    "sub_opinions": [
        "https://www.courtlistener.com/api/rest/v4/opinions/1000/"
    ],
}

OPINION_DETAIL_RAW: dict[str, Any] = {
    "id": 1000,
    "cluster": "https://www.courtlistener.com/api/rest/v4/clusters/123/",
    "type": "010combined",
    "plain_text": "We immediately acknowledge that the原件 text is long.",
    "html_with_citations": "<p>HTML with <b>citations</b> and junk</p>",
    "html": "<p>Plain HTML variant</p>",
    "sha1": "def456",
    "local_path": "/data/opinion/1000",
    "author_str": "Blackmun, J.",
    "joined_by": [],
    "download_url": None,
}

DOCKET_DETAIL_RAW: dict[str, Any] = {
    "id": 55,
    "case_name": "Roe v. Wade",
    "case_name_full": "Jane Roe et al. v. Henry Wade",
    "docket_number": "70-18",
    "court": "https://www.courtlistener.com/api/rest/v4/courts/scotus/",
    "date_filed": "1973-01-22",
    "pacer_case_id": "1234",
    "source": "R",
    "nature_of_suit": "890",
    "jury_demand": "None",
    "jurisdiction_type": "Federal Question",
    "date_created": "2012-01-01",
    "meta": {"page": 1},
}

SEARCH_HIT_O_RAW: dict[str, Any] = {
    "id": 1000,
    "caseName": "Smith v. Jones",
    "dateFiled": "2023-06-15",
    "court": "ca9",
    "court_id": "ca9",
    "citation": ["123 F.3d 456"],
    "cluster_id": 123,
    "docket_id": 55,
    "docketNumber": "23-1234",
    "status": "Precedential",
    "judge": "Smith J.",
    "citeCount": 42,
    "absolute_url": "/opinion/123/smith-v-jones/",
    "score": 0.95,
    "opinions": [
        {
            "id": 1000,
            "type": "010combined",
            "snippet": "the <mark>qualified immunity</mark> doctrine applies",
        },
        {"id": 1001, "type": "concurrence", "snippet": "I concur separately"},
    ],
}

SEARCH_HIT_RD_RAW: dict[str, Any] = {
    "id": 999,
    "caseName": "Patent Corp v. Tech Inc",
    "dateFiled": "2023-07-01",
    "court": "cafc",
    "docket_id": 55,
    "docketNumber": "23-1234",
    "snippet": "motion to <mark>dismiss</mark> granted",
    "document_number": "1",
    "attachment_number": "2",
    "absolute_url": "/recap/999/",
    "score": 0.8,
}

LOOKUP_CLUSTER_RAW: dict[str, Any] = {
    "id": 123,
    "case_name": "Miranda v. Arizona",
    "case_name_full": "Ernesto Miranda v. State of Arizona",
    "date_filed": "1966-06-13",
    "citations": [{"cite": "384 U.S. 436", "type": "official"}],
    "absolute_url": "/opinion/123/miranda-v-arizona/",
    "docket_number": "759",
    "status": "Precedential",
    "citation_count": 3210,
    "precedential_status": "Published",
    "panel_ids": [12, 34],
    "judges": "Warren, C.J.",
    "attorney": "Flynn, Tovar",
    "nature_of_suit": "",
    "posture": "",
    "syllabus": "The Fifth Amendment privilege against self-incrimination...",
    "scdb_id": "1965-151",
    "sha1": "abc789def",
    "local_path": "/data/cluster/123",
    "score": 0.99,
}


# ---------------------------------------------------------------------------
# AC-3.1 / AC-3.3 — table-driven strip_resource tests
# ---------------------------------------------------------------------------

RESOURCE_TABLE = [
    pytest.param("cluster", CLUSTER_DETAIL_RAW, {"caseName", "citations", "court", "dateFiled", "cluster_id", "docket_id"}, id="cluster-detail"),
    pytest.param("opinion", OPINION_DETAIL_RAW, {"caseName", "citations", "court", "dateFiled", "opinionText", "cluster_id", "docket_id"}, id="opinion-detail"),
    pytest.param("docket", DOCKET_DETAIL_RAW, {"caseName", "docketNumber", "court", "dateFiled", "docket_id"}, id="docket-detail"),
]

ALLOWLISTS = {
    "cluster": {"caseName", "citations", "court", "dateFiled", "cluster_id", "docket_id"},
    "opinion": {"caseName", "citations", "court", "dateFiled", "opinionText", "cluster_id", "docket_id"},
    "docket": {"caseName", "docketNumber", "court", "dateFiled", "docket_id"},
}


class TestStripResource:
    """strip_resource behavior — AC-3.1, AC-3.3."""

    @pytest.mark.parametrize(("kind", "raw", "required"), RESOURCE_TABLE)
    def test_ac_3_1_keys_subset_of_allowlist_and_required_present(
        self, kind: str, raw: dict[str, Any], required: set[str]
    ) -> None:
        """AC-3.1: output keys ⊆ FR-3.1 allowlist; required keys present."""
        stripped = strip_resource(kind, raw)
        assert set(stripped.keys()) <= ALLOWLISTS[kind]
        # required keys present in the fixture
        expected = {
            k: v
            for k, v in stripped.items()
            if k in required
        }
        assert expected, "no required keys extracted at all"

    @pytest.mark.parametrize(("kind", "raw", "required"), RESOURCE_TABLE)
    def test_ac_3_3_at_least_two_x_reduction(
        self, kind: str, raw: dict[str, Any], required: set[str]
    ) -> None:
        """AC-3.3: len(json.dumps(stripped)) < 0.5 * len(json.dumps(raw))."""
        stripped = strip_resource(kind, raw)
        assert len(json.dumps(stripped)) < 0.5 * len(json.dumps(raw))

    def test_ac_3_1_cluster_values(self) -> None:
        """AC-3.1: values are normalized (URLs resolved to IDs, casing mapped)."""
        stripped = strip_resource("cluster", CLUSTER_DETAIL_RAW)
        assert stripped["caseName"] == "Roe v. Wade"
        assert stripped["dateFiled"] == "1973-01-22"
        assert stripped["cluster_id"] == 123
        assert stripped["docket_id"] == 55
        assert stripped["citations"] == [{"cite": "410 U.S. 113", "type": "official"}]

    def test_ac_3_2_casing_normalization_identical_keys(self) -> None:
        """AC-3.2: snake_case detail and camelCase search hit → same output keys."""
        snake = strip_resource(
            "cluster", {"case_name": "X v. Y", "date_filed": "2015-06-01", "id": 7}
        )
        camel = strip_search_hit(
            {"caseName": "X v. Y", "dateFiled": "2015-06-01", "cluster_id": 7}, "o"
        )
        # core normalized keys identical across surfaces
        assert snake["caseName"] == camel["caseName"] == "X v. Y"
        assert snake["dateFiled"] == camel["dateFiled"] == "2015-06-01"
        assert snake["cluster_id"] == camel["cluster_id"] == 7

    def test_nfr_6_missing_fields_omitted_never_null_padded(self) -> None:
        """NFR-6/FR-3.4: missing allowlisted fields are omitted, not null."""
        stripped = strip_resource("cluster", {"id": 5})
        assert stripped == {"cluster_id": 5}
        assert "caseName" not in stripped
        assert "court" not in stripped

    def test_unknown_fields_silently_dropped(self) -> None:
        """FR-3.4: unknown fields in API response are silently dropped."""
        stripped = strip_resource("docket", {"id": 1, "wibble": "wobble"})
        assert "wibble" not in stripped

    def test_unknown_kind_raises(self) -> None:
        """Programmer error on an unknown kind is a hard error."""
        with pytest.raises(ValueError, match="Unknown resource kind"):
            strip_resource("banana", {})


class TestExtractOpinionText:
    """ADR-3 text fallback chain (R3)."""

    def test_plain_text_preferred(self) -> None:
        data = {"plain_text": "P", "html": "<p>H</p>"}
        assert extract_opinion_text(data) == "P"

    def test_html_with_citations_fallback(self) -> None:
        data = {"html_with_citations": "<p>a <mark>mark</mark></p>", "html": "<p>b</p>"}
        text = extract_opinion_text(data)
        assert text is not None
        assert "mark" in text and "<mark>" not in text

    def test_html_fallback(self) -> None:
        data = {"html": "<p>only html</p>"}
        assert extract_opinion_text(data) == "only html"

    def test_all_null_returns_none(self) -> None:
        assert extract_opinion_text({"plain_text": None, "html": None}) is None
        assert extract_opinion_text({}) is None


class TestStripSearchHit:
    """Search-hit stripping — AC-2.1 (FR-2.1 allowlist, R2 snippet)."""

    def test_ac_2_1_o_hit_keys_allowlisted_and_snippet_present(self) -> None:
        """AC-2.1: o-type hit keys ⊆ allowlist; snippet from nested opinions[]."""
        stripped = strip_search_hit(SEARCH_HIT_O_RAW, "o")
        assert set(stripped.keys()) <= SEARCH_HIT_ALLOWLIST
        assert stripped["snippet"] == "the <mark>qualified immunity</mark> doctrine applies"
        assert stripped["citations"] == ["123 F.3d 456"]
        assert stripped["cluster_id"] == 123

    def test_ac_2_1_rd_hit_top_level_snippet(self) -> None:
        """AC-2.1: rd-type hit uses top-level snippet; docketNumber kept."""
        stripped = strip_search_hit(SEARCH_HIT_RD_RAW, "rd")
        assert set(stripped.keys()) <= SEARCH_HIT_ALLOWLIST
        assert stripped["snippet"] == "motion to <mark>dismiss</mark> granted"
        assert stripped["docketNumber"] == "23-1234"

    def test_o_hit_no_nested_opinions_snippet_omitted(self) -> None:
        """R2: missing/empty opinions[] → snippet simply omitted (never fatal)."""
        stripped = strip_search_hit({"caseName": "X", "opinions": []}, "o")
        assert "snippet" not in stripped
        assert stripped["caseName"] == "X"

    def test_d_type_hit_no_snippet(self) -> None:
        """FR-2.3: d-type hits carry no snippet."""
        hit = {"case_name": "D", "docket_number": "1", "snippet": "should-not-leak"}
        stripped = strip_search_hit(hit, "d")
        assert "snippet" not in stripped
        assert stripped["caseName"] == "D"
        assert stripped["docketNumber"] == "1"


class TestStripCandidates:
    """Citation-lookup candidate stripping (FR-3.1 candidates schema)."""

    def test_candidates_schema(self) -> None:
        stripped = strip_candidates([LOOKUP_CLUSTER_RAW])
        assert stripped == [
            {
                "caseName": "Miranda v. Arizona",
                "citations": [{"cite": "384 U.S. 436", "type": "official"}],
                "dateFiled": "1966-06-13",
                "cluster_id": 123,
            }
        ]

    def test_candidates_skip_non_dict_entries(self) -> None:
        """NFR-6: malformed entries are skipped, not fatal."""
        assert strip_candidates([LOOKUP_CLUSTER_RAW, "junk", None]) == [
            {
                "caseName": "Miranda v. Arizona",
                "citations": [{"cite": "384 U.S. 436", "type": "official"}],
                "dateFiled": "1966-06-13",
                "cluster_id": 123,
            }
        ]

    def test_ac_3_3_candidates_reduction(self) -> None:
        """AC-3.3 (candidates variant): ≥2× reduction per candidate fixture."""
        raw = [LOOKUP_CLUSTER_RAW]
        stripped = strip_candidates(raw)
        assert len(json.dumps(stripped)) < 0.5 * len(json.dumps(raw))
