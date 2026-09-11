"""Tests for the resolve_citation tool (FR-1, ADR-3).

AC coverage:
- AC-1.1: mocked 200 flow returns cluster id + stripped metadata + opinion
  text, with exactly the expected HTTP calls.
- AC-1.2: mocked 300 returns ≥2 stripped candidates and zero follow-up fetches.
- AC-1.3: mocked 404 returns a structured not-found result, citation echoed,
  no exception.
- AC-1.4: second identical resolution issues zero HTTP requests.
"""

from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

BASE = "https://www.courtlistener.com/api/rest/v4"

LOOKUP_CLUSTER = {
    "id": 123,
    "case_name": "Roe v. Wade",
    "date_filed": "1973-01-22",
    "citations": [{"cite": "410 U.S. 113", "type": "official"}],
    "absolute_url": "/opinion/123/roe-v-wade/",
    "score": 0.99,
}

CLUSTER_2 = {
    "id": 456,
    "case_name": "Roe v. Wade (dissent collection)",
    "date_filed": "1973-01-22",
    "citations": [{"cite": "410 U.S. 113", "type": "parallel"}],
    "absolute_url": "/opinion/456/roe-dissent/",
    "score": 0.85,
}

CLUSTER_DETAIL = {
    "id": 123,
    "case_name": "Roe v. Wade",
    "case_name_full": "Jane Roe et al. v. Henry Wade",
    "docket": f"{BASE}/dockets/55/",
    "date_filed": "1973-01-22",
    "citations": [{"cite": "410 U.S. 113", "type": "official"}],
    "sub_opinions": [f"{BASE}/opinions/1000/", f"{BASE}/opinions/1001/"],
    "absolute_url": "/opinion/123/roe-v-wade/",
    "panel_ids": [7],
}

# court intentionally absent from the cluster detail → exercises the cached
# docket fallback (ADR-3 court rule / risk R1)
DOCKET_DETAIL = {
    "id": 55,
    "case_name": "Roe v. Wade",
    "docket_number": "70-18",
    "court": f"{BASE}/courts/scotus/",
}

OPINION_1000 = {
    "id": 1000,
    "cluster": f"{BASE}/clusters/123/",
    "type": "010combined",
    "plain_text": "The Texas statute makes it a crime to procure an abortion.",
    "html": "<p>plain html</p>",
    "sha1": "abc",
}

OPINION_1001 = {
    "id": 1001,
    "cluster": f"{BASE}/clusters/123/",
    "type": "02dissent",
    "plain_text": "I dissent.",
    "html": None,
    "sha1": "def",
}


def _mock_resolution_flow() -> tuple[respx.Route, respx.Route, respx.Route, respx.Route]:
    """Mock the full 200 resolution flow; returns the four routes for counting."""
    lookup = respx.post(f"{BASE}/citation-lookup/").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "citation": "410 U.S. 113",
                    "normalized_citations": [{"cite": "410 U.S. 113"}],
                    "start_index": 0,
                    "end_index": 12,
                    "status": 200,
                    "error_message": None,
                    "clusters": [LOOKUP_CLUSTER],
                }
            ],
        )
    )
    cluster = respx.get(f"{BASE}/clusters/123/").mock(
        return_value=httpx.Response(200, json=CLUSTER_DETAIL)
    )
    docket = respx.get(f"{BASE}/dockets/55/").mock(
        return_value=httpx.Response(200, json=DOCKET_DETAIL)
    )
    opinion = respx.get(f"{BASE}/opinions/1000/").mock(
        return_value=httpx.Response(200, json=OPINION_1000)
    )
    return lookup, cluster, docket, opinion


class TestResolveCitation200:
    """AC-1.1 — exact resolution of a resolved (200) citation."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_ac_1_1_full_flow_and_call_count(self, client: Client[Any]) -> None:
        """AC-1.1: returns cluster id, stripped metadata, opinion text; exact calls."""
        lookup, cluster, docket, opinion = _mock_resolution_flow()

        async with client:
            result = await client.call_tool(
                "citation_resolve_citation", {"citation": "410 U.S. 113"}
            )

        assert not result.is_error
        data = result.data
        assert data["citation"] == "410 U.S. 113"
        assert data["status"] == 200
        assert data["opinionText"].startswith("The Texas statute")

        cluster_data = data["cluster"]
        assert set(cluster_data.keys()) <= {
            "caseName",
            "citations",
            "court",
            "dateFiled",
            "cluster_id",
            "docket_id",
        }
        assert cluster_data["caseName"] == "Roe v. Wade"
        assert cluster_data["cluster_id"] == 123
        assert cluster_data["docket_id"] == 55
        assert cluster_data["court"] == "scotus"  # via cached docket fallback (R1)
        assert cluster_data["dateFiled"] == "1973-01-22"

        # Exactly the expected HTTP calls: 1 lookup + cluster + docket + opinion.
        assert lookup.call_count == 1
        assert cluster.call_count == 1
        assert docket.call_count == 1
        assert opinion.call_count == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_cluster_carries_court_skips_docket(
        self, client: Client[Any]
    ) -> None:
        """ADR-3/R1: when the cluster exposes court, no docket fetch happens."""
        cluster_with_court = dict(CLUSTER_DETAIL)
        cluster_with_court["court"] = "scotus"
        lookup = respx.post(f"{BASE}/citation-lookup/").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "citation": "410 U.S. 113",
                        "status": 200,
                        "error_message": None,
                        "clusters": [LOOKUP_CLUSTER],
                    }
                ],
            )
        )
        cluster = respx.get(f"{BASE}/clusters/123/").mock(
            return_value=httpx.Response(200, json=cluster_with_court)
        )
        docket = respx.get(f"{BASE}/dockets/55/").mock(
            return_value=httpx.Response(200, json=DOCKET_DETAIL)
        )
        respx.get(f"{BASE}/opinions/1000/").mock(
            return_value=httpx.Response(200, json=OPINION_1000)
        )

        async with client:
            result = await client.call_tool(
                "citation_resolve_citation", {"citation": "410 U.S. 113"}
            )

        assert not result.is_error
        assert result.data["cluster"]["court"] == "scotus"
        assert lookup.call_count == 1
        assert cluster.call_count == 1
        assert docket.call_count == 0  # court present → docket fetch skipped


class TestResolveCitationDispositions:
    """AC-1.2 / AC-1.3 and the 400/429 dispositions (FR-1.2)."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_ac_1_2_ambiguous_300_returns_candidates_no_fetch(
        self, client: Client[Any]
    ) -> None:
        """AC-1.2: 300 → ≥2 stripped candidates, zero cluster/opinion fetches."""
        respx.post(f"{BASE}/citation-lookup/").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "citation": "410 U.S. 113",
                        "status": 300,
                        "error_message": None,
                        "clusters": [LOOKUP_CLUSTER, CLUSTER_DETAIL_FOR(456)],
                    }
                ],
            )
        )
        # No other routes: any follow-up fetch would be rejected by respx
        # (assert_all_mocked) and turn the call into an error.

        async with client:
            result = await client.call_tool(
                "citation_resolve_citation", {"citation": "410 U.S. 113"}
            )

        assert not result.is_error
        data = result.data
        assert data["status"] == 300
        candidates = data["candidates"]
        assert len(candidates) >= 2
        for candidate in candidates:
            assert set(candidate.keys()) <= {"caseName", "citations", "dateFiled", "cluster_id"}
        # No follow-up fetch → no cluster/opinionText payload on the result.
        assert "cluster" not in data
        assert "opinionText" not in data

    @pytest.mark.asyncio
    @respx.mock
    async def test_ac_1_3_404_not_found_no_exception(self, client: Client[Any]) -> None:
        """AC-1.3: 404 → structured not-found with the citation echoed."""
        respx.post(f"{BASE}/citation-lookup/").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "citation": "999 U.S. 9999",
                        "status": 404,
                        "error_message": "Citation not found.",
                        "clusters": [],
                    }
                ],
            )
        )

        async with client:
            result = await client.call_tool(
                "citation_resolve_citation", {"citation": "999 U.S. 9999"}
            )

        assert not result.is_error
        data = result.data
        assert data["status"] == 404
        assert data["found"] is False
        assert data["citation"] == "999 U.S. 9999"

    @pytest.mark.asyncio
    @respx.mock
    async def test_400_invalid_reporter(self, client: Client[Any]) -> None:
        """FR-1.2: 400 → structured invalid result, no exception, no fetch."""
        respx.post(f"{BASE}/citation-lookup/").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "citation": "999 Foo. 9999",
                        "status": 400,
                        "error_message": "Unknown reporter.",
                        "clusters": [],
                    }
                ],
            )
        )

        async with client:
            result = await client.call_tool(
                "citation_resolve_citation", {"citation": "999 Foo. 9999"}
            )

        assert not result.is_error
        data = result.data
        assert data["status"] == 400
        assert data["found"] is False
        assert "Invalid citation" in data["message"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_surfaced_not_retried(self, client: Client[Any]) -> None:
        """FR-1.2: 429 → surfaced; no silent retry."""
        lookup = respx.post(f"{BASE}/citation-lookup/").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "citation": "410 U.S. 113",
                        "status": 429,
                        "error_message": "Over cap.",
                        "clusters": [],
                    }
                ],
            )
        )

        async with client:
            result = await client.call_tool(
                "citation_resolve_citation", {"citation": "410 U.S. 113"}
            )

        assert not result.is_error
        assert result.data["status"] == 429
        assert lookup.call_count == 1  # no retry


class TestResolveCitationCaching:
    """AC-1.4 — repeat resolution issues zero HTTP requests (FR-1.4)."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_ac_1_4_second_resolution_zero_http(self, client: Client[Any]) -> None:
        """AC-1.4: repeat resolution of the same citation → zero HTTP calls."""
        lookup, cluster, docket, opinion = _mock_resolution_flow()

        async with client:
            first = await client.call_tool(
                "citation_resolve_citation", {"citation": "410 U.S. 113"}
            )
            second = await client.call_tool(
                "citation_resolve_citation", {"citation": "410 U.S. 113"}
            )

        assert not first.is_error and not second.is_error
        assert lookup.call_count == 1
        assert cluster.call_count == 1
        assert docket.call_count == 1
        assert opinion.call_count == 1
        assert second.data["cluster"]["caseName"] == "Roe v. Wade"


class TestResolveCitationTextFallback:
    """R3 text fallback chain — never fatal."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_html_fallback_tag_stripped(self, client: Client[Any]) -> None:
        """R3: null plain_text → html_with_citations/html tag-stripped."""
        opinion_null_text = dict(OPINION_1000)
        opinion_null_text["plain_text"] = None
        opinion_null_text["html_with_citations"] = "<p>HTML <mark>cite</mark> text</p>"
        _mock_resolution_flow()
        respx.get(f"{BASE}/opinions/1000/").mock(
            return_value=httpx.Response(200, json=opinion_null_text)
        )

        async with client:
            result = await client.call_tool(
                "citation_resolve_citation", {"citation": "410 U.S. 113"}
            )

        assert not result.is_error
        text = result.data["opinionText"]
        assert "cite" in text and "<mark>" not in text and "<p>" not in text

    @pytest.mark.asyncio
    @respx.mock
    async def test_text_unavailable_note(self, client: Client[Any]) -> None:
        """R3/ADR-3: all text variants null → text_unavailable note, not fatal."""
        opinion_no_text = dict(OPINION_1000)
        opinion_no_text["plain_text"] = None
        opinion_no_text["html"] = None
        _mock_resolution_flow()
        respx.get(f"{BASE}/opinions/1000/").mock(
            return_value=httpx.Response(200, json=opinion_no_text)
        )

        async with client:
            result = await client.call_tool(
                "citation_resolve_citation", {"citation": "410 U.S. 113"}
            )

        assert not result.is_error
        assert result.data["opinionText"] is None
        assert result.data["note"] == "text_unavailable"
        assert result.data["sub_opinions"] == [1000, 1001]

    @pytest.mark.asyncio
    @respx.mock
    async def test_all_opinions_param_fetches_every_sub_opinion(
        self, client: Client[Any]
    ) -> None:
        """ADR-3 step 4: all_opinions=true fetches all sub-opinions (cached)."""
        _mock_resolution_flow()
        respx.get(f"{BASE}/opinions/1001/").mock(
            return_value=httpx.Response(200, json=OPINION_1001)
        )

        async with client:
            result = await client.call_tool(
                "citation_resolve_citation",
                {"citation": "410 U.S. 113", "all_opinions": True},
            )

        assert not result.is_error
        texts = {op["opinion_id"]: op["text"] for op in result.data["opinions"]}
        assert texts[1000].startswith("The Texas statute")
        assert texts[1001] == "I dissent."


class TestReviewFindings:
    """Rework-cycle-1 findings M3/M4 — malformed 200 bodies stay structured."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_review_m3_non_dict_cluster_entry_no_crash(
        self, client: Client[Any]
    ) -> None:
        """M3: a non-dict cluster entry in a 200 body → structured unresolvable result."""
        respx.post(f"{BASE}/citation-lookup/").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "citation": "410 U.S. 113",
                        "status": 200,
                        "error_message": None,
                        "clusters": ["not-a-dict"],
                    }
                ],
            )
        )

        async with client:
            result = await client.call_tool(
                "citation_resolve_citation", {"citation": "410 U.S. 113"}
            )

        assert not result.is_error
        data = result.data
        assert data["found"] is False
        assert data["status"] == 200
        assert "without a usable cluster id" in data["message"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_review_m4_empty_lookup_array_is_not_found(
        self, client: Client[Any]
    ) -> None:
        """M4: an empty citation-lookup array → structured 404-style not-found."""
        respx.post(f"{BASE}/citation-lookup/").mock(
            return_value=httpx.Response(200, json=[])
        )

        async with client:
            result = await client.call_tool(
                "citation_resolve_citation", {"citation": "410 U.S. 113"}
            )

        assert not result.is_error
        data = result.data
        assert data["status"] == 404
        assert data["found"] is False
        assert data["citation"] == "410 U.S. 113"


class TestBatchLookupStripping:
    """D3 — batch_lookup_citations strips candidate clusters, keeps statuses."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_d3_batch_strips_clusters_keeps_statuses(
        self, client: Client[Any]
    ) -> None:
        """D3: candidate clusters stripped to the FR-3.1 candidate schema."""
        respx.post(f"{BASE}/citation-lookup/").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "citation": "410 U.S. 113",
                        "status": 200,
                        "error_message": None,
                        "clusters": [LOOKUP_CLUSTER],
                    },
                    {
                        "citation": "999 U.S. 9999",
                        "status": 404,
                        "error_message": "Not found.",
                        "clusters": [],
                    },
                ],
            )
        )

        async with client:
            result = await client.call_tool(
                "citation_batch_lookup_citations",
                {"citations": ["410 U.S. 113", "999 U.S. 9999"]},
            )

        assert not result.is_error
        data = result.data
        assert data["count"] == 2
        first = data["results"][0]
        assert first["status"] == 200  # per-citation status preserved
        for cluster in first["clusters"]:
            assert set(cluster.keys()) <= {"caseName", "citations", "dateFiled", "cluster_id"}
            assert cluster["cluster_id"] == 123
        assert data["results"][1]["status"] == 404


def CLUSTER_DETAIL_FOR(cluster_id: int) -> dict[str, Any]:
    """Build a second lookup-cluster fixture for 300 responses."""
    return {
        "id": cluster_id,
        "case_name": "Roe v. Wade (dissent collection)",
        "date_filed": "1973-01-22",
        "citations": [{"cite": "410 U.S. 113", "type": "parallel"}],
        "absolute_url": f"/opinion/{cluster_id}/roe-dissent/",
        "score": 0.85,
    }
