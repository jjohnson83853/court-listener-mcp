"""Mocked unit tests for CourtListener MCP server.

These tests use respx to mock HTTP responses, avoiding real API calls.
This enables testing error paths and edge cases reliably.
"""

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

# Sample mock responses
MOCK_OPINIONS_RESPONSE = {
    "count": 2,
    "next": None,
    "previous": None,
    "results": [
        {
            "id": 123456,
            "caseName": "Smith v. Jones",
            "court": "scotus",
            "dateFiled": "2023-06-15",
            "citation": ["123 U.S. 456"],
            "absolute_url": "/opinion/123456/smith-v-jones/",
        },
        {
            "id": 789012,
            "caseName": "Doe v. Roe",
            "court": "scotus",
            "dateFiled": "2023-05-20",
            "citation": ["124 U.S. 789"],
            "absolute_url": "/opinion/789012/doe-v-roe/",
        },
    ],
}

MOCK_DOCKETS_RESPONSE = {
    "count": 1,
    "next": None,
    "previous": None,
    "results": [
        {
            "id": 111222,
            "case_name": "Patent Corp v. Tech Inc",
            "court": "cafc",
            "date_filed": "2023-07-01",
            "docket_number": "23-1234",
        },
    ],
}

MOCK_COURT_RESPONSE = {
    "id": "scotus",
    "full_name": "Supreme Court of the United States",
    "short_name": "SCOTUS",
    "url": "https://www.supremecourt.gov/",
    "in_use": True,
}

MOCK_OPINION_RESPONSE = {
    "id": 123456,
    "absolute_url": "/opinion/123456/smith-v-jones/",
    "cluster": "https://www.courtlistener.com/api/rest/v4/clusters/123/",
    "author": None,
    "plain_text": "This is the opinion text...",
    "html": "<p>This is the opinion text...</p>",
}

MOCK_CLUSTER_DETAIL_RESPONSE = {
    "id": 123,
    "case_name": "Smith v. Jones",
    "case_name_full": "Smith et al. v. Jones et al.",
    "docket": "https://www.courtlistener.com/api/rest/v4/dockets/55/",
    "court": "scotus",
    "date_filed": "2023-06-15",
    "citations": [{"cite": "123 U.S. 456", "type": "official"}],
    "absolute_url": "/opinion/123/smith-v-jones/",
    "panel_ids": [1, 2],
    "judges": "Smith J.",
    "sha1": "abc123",
    "local_path": "/data/cluster/123",
}


class _FakeClock:
    """Deterministic time.time replacement for TTL tests."""

    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

MOCK_CITATION_LOOKUP_RESPONSE = [
    {
        "id": 123456,
        "case_name": "Miranda v. Arizona",
        "absolute_url": "/opinion/123456/miranda-v-arizona/",
        "citation": "384 U.S. 436",
    }
]


class TestMockedSearchTools:
    """Tests for search tools with mocked HTTP responses."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_opinions_success(self, client: Client[Any]) -> None:
        """Test successful opinion search with mocked response."""
        respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            return_value=httpx.Response(200, json=MOCK_OPINIONS_RESPONSE)
        )

        async with client:
            result = await client.call_tool(
                "search_opinions", {"q": "miranda", "court": "scotus", "limit": 5}
            )

            assert not result.is_error
            data = result.data
            assert data["count"] == 2
            assert len(data["results"]) == 2
            assert data["results"][0]["caseName"] == "Smith v. Jones"

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_dockets_success(self, client: Client[Any]) -> None:
        """Test successful docket search with mocked response."""
        respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            return_value=httpx.Response(200, json=MOCK_DOCKETS_RESPONSE)
        )

        async with client:
            result = await client.call_tool(
                "search_dockets", {"q": "patent", "limit": 10}
            )

            assert not result.is_error
            data = result.data
            assert data["count"] == 1
            assert data["results"][0]["caseName"] == "Patent Corp v. Tech Inc"
            assert data["results"][0]["docketNumber"] == "23-1234"

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_empty_results(self, client: Client[Any]) -> None:
        """Test search returning no results."""
        empty_response: dict[str, Any] = {"count": 0, "next": None, "previous": None, "results": []}
        respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            return_value=httpx.Response(200, json=empty_response)
        )

        async with client:
            result = await client.call_tool(
                "search_opinions", {"q": "xyznonexistent123", "limit": 5}
            )

            assert not result.is_error
            data = result.data
            assert data["count"] == 0
            assert len(data["results"]) == 0


class TestSnippetSearch:
    """Snippet-only search behavior (FR-2, ADR-5, D2)."""

    @pytest.mark.asyncio
    async def test_ac_6_1_search_docstrings_have_worked_examples(
        self, client: Client[Any]
    ) -> None:
        """AC-6.1: every search tool's docstring contains a worked typed-param example."""
        async with client:
            tools = await client.list_tools()

        search_tools = [tool for tool in tools if tool.name.startswith("search_")]
        assert len(search_tools) >= 7  # opinions, dockets, r, rd, NL, audio, people
        for tool in search_tools:
            description = (tool.description or "").lower()
            assert "example" in description, f"{tool.name} missing worked example"
            # A typed-param example, not just prose: q=/court=/judge=/query=.
            assert any(
                marker in description for marker in ("q=", "court=", "judge=", "query=")
            ), tool.name

    @pytest.mark.asyncio
    async def test_ac_3_3_include_all_fields_is_real_parameter_default(self) -> None:
        """Checklist (d): include_all_fields has a real False default in the tool signature."""
        import inspect

        from app.tools.get import cluster, docket, opinion

        for tool_fn in (opinion, cluster, docket):
            signature = inspect.signature(tool_fn)
            assert "include_all_fields" in signature.parameters
            assert signature.parameters["include_all_fields"].default is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_review_high_2_unwritable_cache_still_returns_data(
        self, client: Client[Any], tmp_path: Any
    ) -> None:
        """HIGH-2 fix: an unwritable cache dir never turns a good fetch into an error."""
        from app.cache import FileCache, set_cache

        root = tmp_path / "blocked-cache"
        root.mkdir()
        # A FILE where the clusters/ namespace dir must be → every put() raises OSError.
        (root / "clusters").write_text("not a directory", encoding="utf-8")
        set_cache(FileCache(root=root))
        try:
            route = respx.get(
                "https://www.courtlistener.com/api/rest/v4/clusters/123/"
            ).mock(return_value=httpx.Response(200, json=MOCK_CLUSTER_DETAIL_RESPONSE))

            async with client:
                result = await client.call_tool("get_cluster", {"cluster_id": "123"})

            assert not result.is_error
            assert result.data["caseName"] == "Smith v. Jones"
            assert route.call_count == 1  # the API fetch itself succeeded
        finally:
            set_cache(None)

    @pytest.mark.asyncio
    @respx.mock
    async def test_review_court_fallback_via_cached_docket(
        self, client: Client[Any], tmp_path: Any
    ) -> None:
        """T4 court rule (R1): cluster without court → court via cached docket fetch."""
        from app.cache import get_cache

        cluster_no_court = dict(MOCK_CLUSTER_DETAIL_RESPONSE)
        del cluster_no_court["court"]
        cluster_route = respx.get(
            "https://www.courtlistener.com/api/rest/v4/clusters/123/"
        ).mock(return_value=httpx.Response(200, json=cluster_no_court))
        docket_route = respx.get(
            "https://www.courtlistener.com/api/rest/v4/dockets/55/"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 55,
                    "case_name": "Smith v. Jones",
                    "court": "https://www.courtlistener.com/api/rest/v4/courts/dcd/",
                },
            )
        )

        async with client:
            first = await client.call_tool("get_cluster", {"cluster_id": "123"})
            second = await client.call_tool("get_cluster", {"cluster_id": "123"})

        assert not first.is_error and not second.is_error
        assert first.data["court"] == "dcd"
        assert second.data["court"] == "dcd"
        assert cluster_route.call_count == 1
        assert docket_route.call_count == 1  # second call: court served from cache

    @pytest.mark.asyncio
    @respx.mock
    async def test_review_court_fallback_docket_failure_is_not_fatal(
        self, client: Client[Any]
    ) -> None:
        """T4 court rule (R1): a failed docket fetch never fails the tool call."""
        cluster_no_court = dict(MOCK_CLUSTER_DETAIL_RESPONSE)
        del cluster_no_court["court"]
        respx.get(
            "https://www.courtlistener.com/api/rest/v4/clusters/123/"
        ).mock(return_value=httpx.Response(200, json=cluster_no_court))
        respx.get(
            "https://www.courtlistener.com/api/rest/v4/dockets/55/"
        ).mock(return_value=httpx.Response(500, json={"detail": "boom"}))

        async with client:
            result = await client.call_tool("get_cluster", {"cluster_id": "123"})

        assert not result.is_error  # enrichment is best-effort, never fatal
        assert "court" not in result.data  # failed enrichment → court omitted
        assert result.data["caseName"] == "Smith v. Jones"

    @pytest.mark.asyncio
    @respx.mock
    async def test_ac_2_2_default_limit_is_10(self, client: Client[Any]) -> None:
        """AC-2.2: a default-limit opinions call sends hit=10 to the API."""
        route = respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            return_value=httpx.Response(200, json=MOCK_OPINIONS_RESPONSE)
        )

        async with client:
            result = await client.call_tool("search_opinions", {"q": "miranda"})

            assert not result.is_error
            assert route.calls.last.request.url.params["hit"] == "10"

    @pytest.mark.asyncio
    @respx.mock
    async def test_ac_2_2_signature_default_10_cap_50(self, client: Client[Any]) -> None:
        """AC-2.2: the four search tools default to 10 with an explicit 1-50 cap."""
        async with client:
            tools = await client.list_tools()

        schemas = {tool.name: tool.inputSchema for tool in tools}
        for name in (
            "search_opinions",
            "search_dockets",
            "search_dockets_with_documents",
            "search_recap_documents",
        ):
            limit_schema = schemas[name]["properties"]["limit"]
            assert limit_schema.get("default") == 10, name
            assert limit_schema.get("maximum") == 50, name
            assert limit_schema.get("minimum") == 1, name

    @pytest.mark.asyncio
    @respx.mock
    async def test_ac_2_3_highlight_sent_per_type(self, client: Client[Any]) -> None:
        """AC-2.3: highlight=on for o/r/rd; absent for d."""
        respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            return_value=httpx.Response(200, json=MOCK_OPINIONS_RESPONSE)
        )

        async with client:
            await client.call_tool("search_opinions", {"q": "x"})
            assert (
                respx.calls.last.request.url.params.get("highlight") == "on"
            )
            await client.call_tool("search_dockets", {"q": "x"})
            assert respx.calls.last.request.url.params.get("highlight") is None
            await client.call_tool("search_dockets_with_documents", {"q": "x"})
            assert respx.calls.last.request.url.params.get("highlight") == "on"
            await client.call_tool("search_recap_documents", {"q": "x"})
            assert respx.calls.last.request.url.params.get("highlight") == "on"

    @pytest.mark.asyncio
    @respx.mock
    async def test_ac_2_4_search_payload_reduction(self, client: Client[Any]) -> None:
        """AC-2.4: stripped search envelope < 0.5 * raw response size."""
        verbose_response = {
            "count": 1,
            "next": "https://www.courtlistener.com/api/rest/v4/search/?cursor=abc",
            "previous": None,
            "results": [
                {
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
                    "snippet": "the doctrine applies",
                    "opinions": [{"id": 1, "snippet": "the doctrine applies"}],
                    "meta": {"scoring": "bm25", "debug": "x" * 200},
                    "cited_blocks": ["x" * 300],
                }
            ],
        }
        respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            return_value=httpx.Response(200, json=verbose_response)
        )

        async with client:
            result = await client.call_tool("search_opinions", {"q": "doctrine"})

        assert not result.is_error
        assert len(json.dumps(result.data)) < 0.5 * len(json.dumps(verbose_response))

    @pytest.mark.asyncio
    @respx.mock
    async def test_oa_and_people_search_stay_raw(self, client: Client[Any]) -> None:
        """Non-goals: audio (oa) and people (p) searches are raw pass-throughs."""
        raw_audio = {
            "count": 1,
            "next": None,
            "previous": None,
            "results": [{"id": 7, "case_name": "Arg", "download_url": "x.mp3"}],
        }
        respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            return_value=httpx.Response(200, json=raw_audio)
        )

        async with client:
            result = await client.call_tool("search_audio", {"q": "argument"})

            assert not result.is_error
            assert result.data["results"][0]["case_name"] == "Arg"


class TestMockedGetTools:
    """Tests for get tools with mocked HTTP responses."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_court_success(self, client: Client[Any]) -> None:
        """Test successful court retrieval with mocked response."""
        respx.get("https://www.courtlistener.com/api/rest/v4/courts/scotus/").mock(
            return_value=httpx.Response(200, json=MOCK_COURT_RESPONSE)
        )

        async with client:
            result = await client.call_tool("get_court", {"court_id": "scotus"})

            assert not result.is_error
            data = result.data
            assert data["id"] == "scotus"
            assert data["full_name"] == "Supreme Court of the United States"

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_opinion_success(self, client: Client[Any]) -> None:
        """Test opinion retrieval: stripped FR-3.1 output with joined cluster (ADR-4)."""
        respx.get("https://www.courtlistener.com/api/rest/v4/opinions/123456/").mock(
            return_value=httpx.Response(200, json=MOCK_OPINION_RESPONSE)
        )
        respx.get("https://www.courtlistener.com/api/rest/v4/clusters/123/").mock(
            return_value=httpx.Response(200, json=MOCK_CLUSTER_DETAIL_RESPONSE)
        )

        async with client:
            result = await client.call_tool("get_opinion", {"opinion_id": "123456"})

            assert not result.is_error
            data = result.data
            assert set(data.keys()) <= {
                "caseName",
                "citations",
                "court",
                "dateFiled",
                "opinionText",
                "cluster_id",
                "docket_id",
            }
            assert data["opinionText"] == "This is the opinion text..."
            assert data["caseName"] == "Smith v. Jones"
            assert data["cluster_id"] == 123
            assert data["dateFiled"] == "2023-06-15"
            # HIGH-1: docket_id extracted from the joined cluster's docket URL.
            assert data["docket_id"] == 55

    @pytest.mark.asyncio
    @respx.mock
    async def test_ac_4_1_cluster_second_call_zero_http(
        self, client: Client[Any]
    ) -> None:
        """AC-4.1: two consecutive cluster calls for one ID → exactly 1 HTTP request."""
        route = respx.get("https://www.courtlistener.com/api/rest/v4/clusters/123/").mock(
            return_value=httpx.Response(200, json=MOCK_CLUSTER_DETAIL_RESPONSE)
        )

        async with client:
            for _ in range(2):
                result = await client.call_tool("get_cluster", {"cluster_id": "123"})
                assert not result.is_error

        assert route.call_count == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_ac_4_1_opinion_second_call_zero_new_http(
        self, client: Client[Any]
    ) -> None:
        """AC-4.1: opinion (+ its cluster join) fetched once, then cached."""
        opinion_route = respx.get(
            "https://www.courtlistener.com/api/rest/v4/opinions/123456/"
        ).mock(return_value=httpx.Response(200, json=MOCK_OPINION_RESPONSE))
        cluster_route = respx.get(
            "https://www.courtlistener.com/api/rest/v4/clusters/123/"
        ).mock(return_value=httpx.Response(200, json=MOCK_CLUSTER_DETAIL_RESPONSE))

        async with client:
            first = await client.call_tool("get_opinion", {"opinion_id": "123456"})
            second = await client.call_tool("get_opinion", {"opinion_id": "123456"})

        assert not first.is_error and not second.is_error
        # 1 opinion fetch + 1 cluster fetch total; second call fully cached.
        assert opinion_route.call_count == 1
        assert cluster_route.call_count == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_include_all_fields_opt_out_returns_raw(
        self, client: Client[Any]
    ) -> None:
        """D4/FR-3.3: include_all_fields=true returns the raw cached payload."""
        respx.get("https://www.courtlistener.com/api/rest/v4/clusters/123/").mock(
            return_value=httpx.Response(200, json=MOCK_CLUSTER_DETAIL_RESPONSE)
        )

        async with client:
            result = await client.call_tool(
                "get_cluster", {"cluster_id": "123", "include_all_fields": True}
            )

            assert not result.is_error
            data = result.data
            # Raw payload: junk fields intact, no stripping.
            assert data["id"] == 123
            assert "absolute_url" in data
            assert "case_name" in data

    @pytest.mark.asyncio
    @respx.mock
    async def test_ac_4_2_cache_files_under_expected_paths(
        self, client: Client[Any]
    ) -> None:
        """AC-4.2: a cluster get writes clusters/cluster-<id>.json."""
        from app.cache import get_cache

        respx.get("https://www.courtlistener.com/api/rest/v4/clusters/123/").mock(
            return_value=httpx.Response(200, json=MOCK_CLUSTER_DETAIL_RESPONSE)
        )

        async with client:
            await client.call_tool("get_cluster", {"cluster_id": "123"})

        cache = get_cache()
        assert (cache.root / "clusters" / "cluster-123.json").is_file()

    @pytest.mark.asyncio
    @respx.mock
    async def test_ac_4_3_expired_entry_triggers_fresh_http(
        self,
        client: Client[Any],
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC-4.3: past TTL the cached entry misses → fresh HTTP call."""
        import app.cache as cache_module
        from app.cache import FileCache, set_cache

        fake_time = _FakeClock(1_000_000.0)
        monkeypatch.setattr(cache_module.time, "time", fake_time)
        set_cache(FileCache(root=tmp_path / "cache", ttl_static=3600))
        try:
            route = respx.get(
                "https://www.courtlistener.com/api/rest/v4/clusters/123/"
            ).mock(return_value=httpx.Response(200, json=MOCK_CLUSTER_DETAIL_RESPONSE))

            async with client:
                await client.call_tool("get_cluster", {"cluster_id": "123"})
                assert route.call_count == 1
                await client.call_tool("get_cluster", {"cluster_id": "123"})
                assert route.call_count == 1  # fresh entry: still cached

                fake_time.now += 3601  # age > ttl → expired → fresh HTTP
                await client.call_tool("get_cluster", {"cluster_id": "123"})
                assert route.call_count == 2
        finally:
            set_cache(None)

    @pytest.mark.asyncio
    @respx.mock
    async def test_ac_4_6_no_key_material_in_cache_after_flow(
        self, client: Client[Any]
    ) -> None:
        """AC-4.6: end-to-end get flow writes cache files without auth material."""
        from app.cache import get_cache as _get_cache

        respx.get("https://www.courtlistener.com/api/rest/v4/clusters/123/").mock(
            return_value=httpx.Response(200, json=MOCK_CLUSTER_DETAIL_RESPONSE)
        )

        async with client:
            await client.call_tool("get_cluster", {"cluster_id": "123"})

        for path in _get_cache().root.rglob("*.json"):
            content = path.read_text(encoding="utf-8")
            assert "test-key-offline-dummy" not in content
            assert "Authorization" not in content
            assert "Token " not in content


class TestErrorHandling:
    """Tests for HTTP error handling."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_401_unauthorized(self, client: Client[Any]) -> None:
        """Test handling of 401 Unauthorized response."""
        respx.get("https://www.courtlistener.com/api/rest/v4/courts/scotus/").mock(
            return_value=httpx.Response(
                401, json={"detail": "Authentication credentials were not provided."}
            )
        )

        async with client:
            with pytest.raises(ToolError):
                await client.call_tool("get_court", {"court_id": "scotus"})

    @pytest.mark.asyncio
    @respx.mock
    async def test_403_forbidden(self, client: Client[Any]) -> None:
        """Test handling of 403 Forbidden response."""
        respx.get("https://www.courtlistener.com/api/rest/v4/opinions/123/").mock(
            return_value=httpx.Response(
                403, json={"detail": "You do not have permission to perform this action."}
            )
        )

        async with client:
            with pytest.raises(ToolError):
                await client.call_tool("get_opinion", {"opinion_id": "123"})

    @pytest.mark.asyncio
    @respx.mock
    async def test_404_not_found(self, client: Client[Any]) -> None:
        """Test handling of 404 Not Found response."""
        respx.get("https://www.courtlistener.com/api/rest/v4/opinions/999999999/").mock(
            return_value=httpx.Response(404, json={"detail": "Not found."})
        )

        async with client:
            with pytest.raises(ToolError):
                await client.call_tool("get_opinion", {"opinion_id": "999999999"})

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_rate_limited(self, client: Client[Any]) -> None:
        """Test handling of 429 Too Many Requests response."""
        respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            return_value=httpx.Response(
                429,
                json={"detail": "Request was throttled. Expected available in 60 seconds."},
                headers={"Retry-After": "60"},
            )
        )

        async with client:
            with pytest.raises(ToolError):
                await client.call_tool("search_opinions", {"q": "test"})

    @pytest.mark.asyncio
    @respx.mock
    async def test_500_server_error(self, client: Client[Any]) -> None:
        """Test handling of 500 Internal Server Error response."""
        respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            return_value=httpx.Response(500, json={"detail": "Internal server error."})
        )

        async with client:
            with pytest.raises(ToolError):
                await client.call_tool("search_dockets", {"q": "test"})

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_error(self, client: Client[Any]) -> None:
        """Test handling of request timeout."""
        respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            side_effect=httpx.TimeoutException("Connection timed out")
        )

        async with client:
            with pytest.raises(ToolError):
                await client.call_tool("search_opinions", {"q": "test"})

    @pytest.mark.asyncio
    @respx.mock
    async def test_connection_error(self, client: Client[Any]) -> None:
        """Test handling of connection error."""
        respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        async with client:
            with pytest.raises(ToolError):
                await client.call_tool("search_opinions", {"q": "test"})


class TestMockedCitationTools:
    """Tests for citation tools with mocked HTTP responses."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_lookup_citation_success(self, client: Client[Any]) -> None:
        """Test successful citation lookup with mocked response."""
        respx.post("https://www.courtlistener.com/api/rest/v4/citation-lookup/").mock(
            return_value=httpx.Response(200, json=MOCK_CITATION_LOOKUP_RESPONSE)
        )

        async with client:
            result = await client.call_tool(
                "citation_lookup_citation", {"citation": "384 U.S. 436"}
            )

            assert not result.is_error
            data = result.data
            assert data["count"] == 1
            assert data["results"][0]["case_name"] == "Miranda v. Arizona"

    @pytest.mark.asyncio
    @respx.mock
    async def test_batch_lookup_citations_success(self, client: Client[Any]) -> None:
        """Test successful batch citation lookup with mocked response."""
        batch_response = [
            {"id": 1, "case_name": "Case One", "citation": "100 U.S. 1"},
            {"id": 2, "case_name": "Case Two", "citation": "200 U.S. 2"},
        ]
        respx.post("https://www.courtlistener.com/api/rest/v4/citation-lookup/").mock(
            return_value=httpx.Response(200, json=batch_response)
        )

        async with client:
            result = await client.call_tool(
                "citation_batch_lookup_citations",
                {"citations": ["100 U.S. 1", "200 U.S. 2"]},
            )

            assert not result.is_error
            data = result.data
            assert data["count"] == 2

    @pytest.mark.asyncio
    async def test_verify_citation_format_valid(self, client: Client[Any]) -> None:
        """Test citation format verification with valid citation."""
        async with client:
            result = await client.call_tool(
                "citation_verify_citation_format", {"citation": "410 U.S. 113"}
            )

            assert not result.is_error
            data = result.data
            assert data["valid"] is True
            assert data["citation"] == "410 U.S. 113"

    @pytest.mark.asyncio
    async def test_verify_citation_format_invalid(self, client: Client[Any]) -> None:
        """Test citation format verification with invalid citation."""
        async with client:
            result = await client.call_tool(
                "citation_verify_citation_format", {"citation": "not a real citation xyz"}
            )

            assert not result.is_error
            data = result.data
            assert data["valid"] is False
            assert len(data["issues"]) > 0

    @pytest.mark.asyncio
    async def test_verify_citation_format_empty(self, client: Client[Any]) -> None:
        """Test citation format verification with empty citation."""
        async with client:
            result = await client.call_tool(
                "citation_verify_citation_format", {"citation": "   "}
            )

            assert not result.is_error
            data = result.data
            assert data["valid"] is False
            assert "Citation is empty" in data["issues"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_enhanced_citation_lookup_success(self, client: Client[Any]) -> None:
        """Test enhanced citation lookup combining citeurl and CourtListener."""
        respx.post("https://www.courtlistener.com/api/rest/v4/citation-lookup/").mock(
            return_value=httpx.Response(200, json=MOCK_CITATION_LOOKUP_RESPONSE)
        )

        async with client:
            result = await client.call_tool(
                "citation_enhanced_citation_lookup",
                {"citation": "384 U.S. 436", "include_courtlistener": True},
            )

            assert not result.is_error
            data = result.data
            assert data["citation"] == "384 U.S. 436"
            assert "citeurl_analysis" in data
            assert "courtlistener_data" in data
            assert "combined_info" in data

    @pytest.mark.asyncio
    async def test_enhanced_citation_lookup_citeurl_only(
        self, client: Client[Any]
    ) -> None:
        """Test enhanced citation lookup with citeurl only (no API call)."""
        async with client:
            result = await client.call_tool(
                "citation_enhanced_citation_lookup",
                {"citation": "410 U.S. 113", "include_courtlistener": False},
            )

            assert not result.is_error
            data = result.data
            assert data["citeurl_analysis"]["success"] is True
            # CourtListener data should be empty when not requested
            assert data["courtlistener_data"] == {}


class TestSearchFilters:
    """Tests for search tool filter parameters with mocked responses."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_with_all_opinion_filters(self, client: Client[Any]) -> None:
        """Test opinion search with all available filters."""
        respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            return_value=httpx.Response(200, json=MOCK_OPINIONS_RESPONSE)
        )

        async with client:
            result = await client.call_tool(
                "search_opinions",
                {
                    "q": "constitutional",
                    "court": "scotus",
                    "case_name": "test",
                    "judge": "Roberts",
                    "filed_after": "2020-01-01",
                    "filed_before": "2023-12-31",
                    "cited_gt": 10,
                    "cited_lt": 1000,
                    "order_by": "dateFiled desc",
                    "limit": 25,
                },
            )

            assert not result.is_error

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_recap_documents(self, client: Client[Any]) -> None:
        """Test RECAP document search with mocked response."""
        recap_response = {
            "count": 1,
            "results": [
                {
                    "id": 555,
                    "description": "Motion to dismiss",
                    "document_number": "5",
                    "attachment_number": None,
                }
            ],
        }
        respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            return_value=httpx.Response(200, json=recap_response)
        )

        async with client:
            result = await client.call_tool(
                "search_recap_documents",
                {"q": "motion to dismiss", "court": "dcd", "limit": 10},
            )

            assert not result.is_error
            data = result.data
            assert data["count"] == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_audio(self, client: Client[Any]) -> None:
        """Test audio search with mocked response."""
        audio_response = {
            "count": 1,
            "results": [
                {
                    "id": 777,
                    "case_name": "Test Oral Argument",
                    "date_argued": "2023-05-01",
                    "duration": 3600,
                }
            ],
        }
        respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            return_value=httpx.Response(200, json=audio_response)
        )

        async with client:
            result = await client.call_tool(
                "search_audio",
                {"q": "oral argument", "court": "scotus", "limit": 5},
            )

            assert not result.is_error

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_people(self, client: Client[Any]) -> None:
        """Test people search with mocked response."""
        people_response = {
            "count": 1,
            "results": [
                {
                    "id": 999,
                    "name_first": "John",
                    "name_last": "Roberts",
                    "position_type": "jud",
                }
            ],
        }
        respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            return_value=httpx.Response(200, json=people_response)
        )

        async with client:
            result = await client.call_tool(
                "search_people",
                {"q": "Roberts", "position_type": "jud", "limit": 5},
            )

            assert not result.is_error


class TestGetTools:
    """Additional tests for get tools with mocked responses."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_docket(self, client: Client[Any]) -> None:
        """Test docket retrieval returns the stripped FR-3.1 docket shape."""
        docket_response = {
            "id": 12345,
            "case_name": "Test Case",
            "docket_number": "1:23-cv-00001",
            "court": "https://www.courtlistener.com/api/rest/v4/courts/dcd/",
        }
        respx.get("https://www.courtlistener.com/api/rest/v4/dockets/12345/").mock(
            return_value=httpx.Response(200, json=docket_response)
        )

        async with client:
            result = await client.call_tool("get_docket", {"docket_id": "12345"})

            assert not result.is_error
            data = result.data
            assert set(data.keys()) <= {"caseName", "docketNumber", "court", "dateFiled", "docket_id"}
            assert data["docket_id"] == 12345
            assert data["caseName"] == "Test Case"
            assert data["docketNumber"] == "1:23-cv-00001"
            assert data["court"] == "dcd"

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_audio(self, client: Client[Any]) -> None:
        """Test audio retrieval with mocked response."""
        audio_response = {
            "id": 67890,
            "case_name": "Oral Argument Recording",
            "date_argued": "2023-10-01",
            "download_url": "https://example.com/audio.mp3",
        }
        respx.get("https://www.courtlistener.com/api/rest/v4/audio/67890/").mock(
            return_value=httpx.Response(200, json=audio_response)
        )

        async with client:
            result = await client.call_tool("get_audio", {"audio_id": "67890"})

            assert not result.is_error
            data = result.data
            assert data["id"] == 67890

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_cluster(self, client: Client[Any]) -> None:
        """Test cluster retrieval returns the stripped FR-3.1 cluster shape."""
        cluster_response = {
            "id": 11111,
            "case_name": "Smith v. Jones",
            "date_filed": "2023-06-15",
            "citations": [{"volume": 123, "reporter": "U.S.", "page": 456}],
        }
        respx.get("https://www.courtlistener.com/api/rest/v4/clusters/11111/").mock(
            return_value=httpx.Response(200, json=cluster_response)
        )

        async with client:
            result = await client.call_tool("get_cluster", {"cluster_id": "11111"})

            assert not result.is_error
            data = result.data
            assert set(data.keys()) <= {"caseName", "citations", "court", "dateFiled", "cluster_id", "docket_id"}
            assert data["cluster_id"] == 11111
            assert data["caseName"] == "Smith v. Jones"

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_person(self, client: Client[Any]) -> None:
        """Test person retrieval with mocked response."""
        person_response = {
            "id": 22222,
            "name_first": "Ruth",
            "name_last": "Ginsburg",
            "date_dob": "1933-03-15",
            "positions": [],
        }
        respx.get("https://www.courtlistener.com/api/rest/v4/people/22222/").mock(
            return_value=httpx.Response(200, json=person_response)
        )

        async with client:
            result = await client.call_tool("get_person", {"person_id": "22222"})

            assert not result.is_error
            data = result.data
            assert data["id"] == 22222
            assert data["name_last"] == "Ginsburg"
