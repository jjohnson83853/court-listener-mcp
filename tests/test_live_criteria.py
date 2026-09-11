"""Mocked AC/NFR acceptance tests (requirements.md AC-1.5, AC-2.5, AC-4.7, NFR-1).

This file replaces the former live ``tests/test_integration.py``: the same
four acceptance tests, but every HTTP interaction is mocked with respx (same
patterns as tests/test_mocked.py and tests/test_resolve_citation.py) and no
``@pytest.mark.integration`` marker, so they run in the default offline suite.
Agents must never call the live CourtListener service (user directive) — an
unmocked HTTP request would raise inside respx and fail the test.

Ported assertion logic (from the live bodies, onto mocked transports):

- AC-1.5: ``410 U.S. 113`` resolves to a Roe v. Wade-shaped cluster + opinion
  text; ``999 U.S. 9999`` returns a structured not-found result (no
  exception) and issues NO cluster/opinion/docket HTTP (respx route counts).
- AC-2.5: a snippet search returns ≤10 hits restricted to the FR-2.1
  allowlist with nested ``opinions[0].snippet`` (R2); one hit's cluster_id
  fetches full metadata once — the second identical call is cache-served
  (route count stays 1, data identical).
- AC-4.7: after a fetch, a fresh ``FileCache`` on the SAME root (the
  ``get_cache``/``set_cache`` DI seam) serves the cluster with zero HTTP —
  asserted via respx route-count deltas AND a spy on the designated raw fetch
  helper (``app.tools.get._fetch_resource``, AC-3.4).
- NFR-1: one realistic opinion fixture proves the stripped JSON is < 0.5× the
  raw payload size.

Realism follows spec/design.md §1.2: search hits are camelCase with the
snippet nested in ``opinions[0].snippet``; detail endpoints are snake_case;
citation-lookup and cluster objects carry no ``court`` (risk R1 → cached
docket fallback).

Test isolation follows the same conftest fixtures as the rest of the offline
suite: the autouse ``_isolated_cache`` fixture binds the cache to ``tmp_path``
so these tests never touch the repo's real ``.cache/``.
"""

import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from fastmcp import Client
from loguru import logger

from app.cache import FileCache, get_cache, set_cache
from app.fields import SEARCH_HIT_ALLOWLIST
from app.tools import get as get_module

BASE = "https://www.courtlistener.com/api/rest/v4"

# ---------------------------------------------------------------------------
# AC-1.5 / AC-4.7 fixtures — the Roe v. Wade resolution flow. Citation-lookup
# cluster objects are snake_case without court (§1.2); the cluster detail
# carries a docket URL and no court, exercising the cached docket fallback
# (risk R1). IDs mirror the real Roe record for realism.
# ---------------------------------------------------------------------------

ROE_LOOKUP_BODY: list[dict[str, Any]] = [
    {
        "citation": "410 U.S. 113",
        "normalized_citations": [{"cite": "410 U.S. 113"}],
        "start_index": 0,
        "end_index": 12,
        "status": 200,
        "error_message": None,
        "clusters": [
            {
                "id": 6605,
                "case_name": "Roe v. Wade",
                "case_name_full": "Jane ROE, et al., Appellants, v. Henry WADE",
                "absolute_url": "/opinion/6605/roe-v-wade/",
                "date_filed": "1973-01-22",
                "citations": [{"cite": "410 U.S. 113", "type": "official"}],
                "score": 0.9999,
            }
        ],
    }
]

NOT_FOUND_BODY: list[dict[str, Any]] = [
    {
        "citation": "999 U.S. 9999",
        "normalized_citations": [{"cite": "999 U.S. 9999"}],
        "start_index": 0,
        "end_index": 13,
        "status": 404,
        "error_message": "Citation not found.",
        "clusters": [],
    }
]

ROE_CLUSTER_DETAIL: dict[str, Any] = {
    "id": 6605,
    "case_name": "Roe v. Wade",
    "case_name_full": "Jane ROE, et al., Appellants, v. Henry WADE",
    "docket": f"{BASE}/dockets/2759/",
    "date_filed": "1973-01-22",
    "citations": [{"cite": "410 U.S. 113", "type": "official"}],
    "sub_opinions": [f"{BASE}/opinions/66051/"],
    "absolute_url": "/opinion/6605/roe-v-wade/",
    "panel_ids": [152],
}

ROE_DOCKET: dict[str, Any] = {
    "id": 2759,
    "case_name": "Roe v. Wade",
    "docket_number": "70-18",
    "court": f"{BASE}/courts/scotus/",
}

ROE_OPINION: dict[str, Any] = {
    "id": 66051,
    "cluster": f"{BASE}/clusters/6605/",
    "type": "010combined",
    "plain_text": (
        "We forthwith acknowledge our appreciation of the Fourteenth Amendment: "
        "the right of privacy, though not explicitly mentioned in the "
        "Constitution, is broad enough to encompass a woman's decision whether "
        "or not to terminate her pregnancy."
    ),
    "html": (
        "<p>We forthwith acknowledge our appreciation of the Fourteenth "
        "Amendment: the right of privacy...</p>"
    ),
    "sha1": "6f4a" + "c" * 36,
}


def _dispatch_lookup(request: httpx.Request) -> httpx.Response:
    """Return the Roe 200 entry for 410 U.S. 113, the 404 entry for 999 U.S. 9999."""
    text = parse_qs(request.content.decode("utf-8")).get("text", [""])[0]
    if "999" in text:
        return httpx.Response(200, json=NOT_FOUND_BODY)
    return httpx.Response(200, json=ROE_LOOKUP_BODY)


def _mock_roe_resolution_flow() -> tuple[respx.Route, respx.Route, respx.Route, respx.Route]:
    """Mock the full Roe resolution flow; returns routes for call-count asserts."""
    lookup = respx.post(f"{BASE}/citation-lookup/").mock(side_effect=_dispatch_lookup)
    cluster = respx.get(f"{BASE}/clusters/6605/").mock(
        return_value=httpx.Response(200, json=ROE_CLUSTER_DETAIL)
    )
    docket = respx.get(f"{BASE}/dockets/2759/").mock(
        return_value=httpx.Response(200, json=ROE_DOCKET)
    )
    opinion = respx.get(f"{BASE}/opinions/66051/").mock(
        return_value=httpx.Response(200, json=ROE_OPINION)
    )
    return lookup, cluster, docket, opinion


# ---------------------------------------------------------------------------
# AC-2.5 fixtures — ten realistic camelCase v4 o-type search hits (snippets
# nested in opinions[0].snippet per R2, junk fields that stripping must drop)
# plus a snake_case cluster detail without court (R1 → cached docket fallback).
# ---------------------------------------------------------------------------

_QI_CASES: list[tuple[str, str, str, str]] = [
    # (caseName, official citation, dateFiled, docketNumber)
    ("Saucier v. Katz", "533 U.S. 194", "2001-06-07", "99-4"),
    ("Pearson v. Callahan", "555 U.S. 223", "2009-01-21", "07-751"),
    ("Hope v. Pelzer", "536 U.S. 730", "2002-06-17", "01-1120"),
    ("Anderson v. Creighton", "483 U.S. 635", "1987-06-24", "86-20"),
    ("Malley v. Briggs", "475 U.S. 335", "1986-03-03", "84-1411"),
    ("Mitchell v. Forsyth", "472 U.S. 511", "1985-06-14", "83-1832"),
    ("Harlow v. Fitzgerald", "457 U.S. 800", "1982-06-24", "80-1793"),
    ("Brosseau v. Haugen", "543 U.S. 194", "2004-11-08", "03-772"),
    ("Kisela v. Hughes", "584 U.S. 100", "2018-05-27", "17-467"),
    ("Wood v. Moss", "572 U.S. 361", "2014-06-02", "13-209"),
]


def _o_type_hit(index: int) -> dict[str, Any]:
    """Build one documented v4 o-type search hit (camelCase, nested snippet)."""
    case_name, cite, date_filed, docket_number = _QI_CASES[index]
    cluster_id = 700 + index
    return {
        "id": 9000 + index,
        "caseName": case_name,
        "dateFiled": date_filed,
        "citation": [cite],
        "cluster_id": cluster_id,
        "docket_id": 800 + index,
        "court": "scotus",
        "court_id": "scotus",
        "docketNumber": docket_number,
        "judge": "White J.",
        "citeCount": 900 - index,
        "status": "Precedential",
        "score": 0.95 - index * 0.01,
        "absolute_url": f"/opinion/{cluster_id}/qi-case-{index}/",
        "opinions": [
            {
                "id": 9100 + index,
                "snippet": (
                    f"the <mark>qualified immunity</mark> doctrine shields "
                    f"{case_name}'s officials"
                ),
            }
        ],
        "meta": {"scoring": "bm25", "debug_pad": "x" * 32},
        "cited_blocks": [f"block {index} of the qualified-immunity analysis " * 2],
    }


_SEARCH_ENVELOPE: dict[str, Any] = {
    "count": 10,
    "next": None,
    "previous": None,
    "results": [_o_type_hit(i) for i in range(10)],
}

# The cluster behind the first hit (Saucier, cluster 700) — snake_case detail
# with a docket URL and no court (§1.2 / risk R1).
SAUCIER_CLUSTER_DETAIL: dict[str, Any] = {
    "id": 700,
    "case_name": "Saucier v. Katz",
    "case_name_full": "Estate of Katz v. Saucier",
    "docket": f"{BASE}/dockets/800/",
    "date_filed": "2001-06-07",
    "citations": [{"cite": "533 U.S. 194", "type": "official"}],
    "sub_opinions": [f"{BASE}/opinions/9100/"],
    "absolute_url": "/opinion/700/saucier-v-katz/",
    "panel_ids": [3, 4],
}

SAUCIER_DOCKET: dict[str, Any] = {
    "id": 800,
    "case_name": "Saucier v. Katz",
    "docket_number": "99-4",
    "court": f"{BASE}/courts/scotus/",
}

# ---------------------------------------------------------------------------
# NFR-1 fixture — one realistic, verbose snake_case opinion detail whose html
# variants and metadata junk dominate the raw payload size, with its joined
# cluster + docket (court fallback) for the stripped (default) call.
# ---------------------------------------------------------------------------

_NFR_PLAIN_TEXT = (
    "We granted certiorari in this case to reconsider the constitutional "
    "limitations on the criminalization of abortion. The Texas statutes that "
    "penalize the procuring of an abortion by a licensed physician are before "
    "the Court. The appellant contends that the statutes are unconstitutionally "
    "vague and that they abridge the right of personal privacy, which is "
    "protected by the Fourteenth Amendment. The Court holds that the right of "
    "privacy, though not explicitly mentioned in the Constitution, is broad "
    "enough to encompass a woman's decision whether or not to terminate her "
    "pregnancy. A state criminal abortion statute that excepts from criminality "
    "only a life-saving procedure on the mother's behalf, without regard to "
    "pregnancy stage and without recognition of the other interests involved, "
    "is violative of the Due Process Clause of the Fourteenth Amendment. "
)

_NFR_HTML_WITH_CITATIONS = (
    "<p>We granted certiorari in this case to reconsider the constitutional "
    "<mark>limitations</mark> on the criminalization of abortion. The Texas "
    "statutes that penalize the procuring of an abortion are before the Court. "
    "See, e.g., <mark>Griswold v. Connecticut</mark>, 381 U.S. 479 (1965). The "
    "Court holds that the right of privacy is broad enough to encompass a "
    "woman's decision whether or not to terminate her pregnancy.</p>"
)


def _nfr_opinion_detail() -> dict[str, Any]:
    """Realistic raw opinion detail (§1.2: snake_case + metadata junk)."""
    return {
        "id": 123456,
        "type": "010combined",
        "cluster": f"{BASE}/clusters/123/",
        "author": f"{BASE}/people/9/",
        "joined_by": [f"{BASE}/people/10/", f"{BASE}/people/11/"],
        "plain_text": _NFR_PLAIN_TEXT,
        "html": "<p>" + _NFR_PLAIN_TEXT + "</p>",
        "html_with_citations": _NFR_HTML_WITH_CITATIONS,
        "html_lawbox": None,
        "html_columbia": None,
        "sha1": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b7852b855",
        "date_created": "1973-01-22T05:00:00Z",
        "date_modified": "2021-03-04T12:00:00Z",
        "extracted_by_ocr": True,
        "local_path": "/data/opinions/123456/roe-v-wade-1973.html",
        "download_url": "https://storage.courtlistener.com/pdf/1973/01/22/roe_v_wade.pdf",
        "absolute_url": "/opinion/123456/roe-v-wade/",
        "ordering_key": 1,
        "views": {"count": 15234, "recent": 321},
        "author_str": "Blackmun, J.",
        "per_curiam": False,
        "page_count": 53,
    }


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_ac_1_5_roe_resolves_999_not_found(client: Client[Any]) -> None:
    """AC-1.5: '410 U.S. 113' → Roe cluster + text; '999' → structured not-found.

    The negative branch raises no exception and issues NO cluster/opinion
    HTTP — every detail route's call count is unchanged after the bogus call.

    Args:
        client: The FastMCP test client fixture.

    """
    lookup, cluster_route, docket_route, opinion_route = _mock_roe_resolution_flow()

    async with client:
        roe = await client.call_tool(
            "citation_resolve_citation", {"citation": "410 U.S. 113"}
        )
        assert not roe.is_error
        data = roe.data

        assert data["citation"] == "410 U.S. 113"
        assert data["status"] == 200

        cluster = data["cluster"]
        assert set(cluster.keys()) <= {
            "caseName",
            "citations",
            "court",
            "dateFiled",
            "cluster_id",
            "docket_id",
        }
        case_name = str(cluster.get("caseName", ""))
        assert "roe" in case_name.lower(), f"Expected Roe in caseName, got {case_name!r}"
        assert "wade" in case_name.lower(), f"Expected Wade in caseName, got {case_name!r}"
        assert cluster.get("cluster_id") is not None
        assert cluster["court"] == "scotus"  # via cached docket fallback (R1)

        opinion_text = data.get("opinionText")
        assert isinstance(opinion_text, str) and opinion_text.strip(), (
            "AC-1.5 requires opinion text for the resolved cluster"
        )

        # Well-formed but non-existent citation → structured not-found, no exception.
        bogus = await client.call_tool(
            "citation_resolve_citation", {"citation": "999 U.S. 9999"}
        )
        assert not bogus.is_error
        not_found = bogus.data

        assert not_found["citation"] == "999 U.S. 9999"  # citation echoed back
        assert not_found["found"] is False
        assert not_found["status"] in (400, 404)  # invalid reporter OR valid-format not-found
        assert not_found.get("message")

        # No follow-up HTTP for the not-found branch: exactly two lookup POSTs
        # (one per citation — distinct slugs) and the Roe-side detail fetches
        # only. Cluster/opinion/docket counts unchanged by the bogus call.
        assert lookup.call_count == 2
        assert cluster_route.call_count == 1
        assert docket_route.call_count == 1
        assert opinion_route.call_count == 1
        logger.info(
            f"AC-1.5 (mocked): 410 U.S. 113 → {case_name!r} "
            f"({len(opinion_text)} chars of text); 999 U.S. 9999 → "
            f"status {not_found['status']} (structured, no exception, zero follow-up HTTP)"
        )


@pytest.mark.asyncio
@respx.mock
async def test_ac_2_5_snippet_search_then_fetch_once(client: Client[Any]) -> None:
    """AC-2.5: snippet search → ≤10 allowlisted hits; cluster fetched once.

    Args:
        client: The FastMCP test client fixture.

    """
    search_route = respx.get(f"{BASE}/search/").mock(
        return_value=httpx.Response(200, json=_SEARCH_ENVELOPE)
    )
    cluster_route = respx.get(f"{BASE}/clusters/700/").mock(
        return_value=httpx.Response(200, json=SAUCIER_CLUSTER_DETAIL)
    )
    docket_route = respx.get(f"{BASE}/dockets/800/").mock(
        return_value=httpx.Response(200, json=SAUCIER_DOCKET)
    )

    async with client:
        search = await client.call_tool(
            "search_opinions", {"q": "qualified immunity", "court": "scotus"}
        )
        assert not search.is_error
        sdata = search.data

        # The AC's exact request reached the API, with highlighting (FR-2.3).
        assert search_route.calls.last.request.url.params["q"] == "qualified immunity"
        assert search_route.calls.last.request.url.params["court"] == "scotus"
        assert search_route.calls.last.request.url.params["highlight"] == "on"

        # ADR-5 envelope; default limit is 10 (FR-2.2) so hits stay ≤10.
        assert "count" in sdata and "results" in sdata
        results = sdata["results"]
        assert isinstance(results, list)
        assert len(results) <= 10, f"Expected ≤10 hits, got {len(results)}"
        assert sdata["count"] == 10 and len(results) == 10

        # FR-2.1 allowlist: every hit carries only allowlisted keys, and the
        # snippet came from the nested opinions[0].snippet (R2) verbatim.
        for hit in results:
            assert set(hit.keys()) <= set(SEARCH_HIT_ALLOWLIST), (
                f"Hit keys {sorted(hit.keys())} outside the FR-2.1 allowlist"
            )
            assert isinstance(hit.get("snippet"), str) and hit["snippet"].strip()

        assert results[0]["snippet"] == (
            "the <mark>qualified immunity</mark> doctrine shields "
            "Saucier v. Katz's officials"
        )

        cluster_id = results[0].get("cluster_id")
        assert cluster_id is not None, f"No cluster_id on first hit: {results[0]}"

        # Fetch the chosen cluster's full metadata once (mocked HTTP).
        first = await client.call_tool("get_cluster", {"cluster_id": str(cluster_id)})
        assert not first.is_error
        first_data = first.data
        assert first_data.get("caseName") == "Saucier v. Katz"
        assert first_data.get("court") == "scotus"  # via cached docket fallback (R1)

        # Fetch-once evidence: the cluster cache file exists under the expected
        # FR-4.1 namespace path, and an identical second call returns identical
        # (cache-served) data with the cluster route count staying at 1.
        cache_root = get_cache().root
        cache_file = cache_root / "clusters" / f"cluster-{cluster_id}.json"
        assert cache_file.exists(), f"Expected cache file at {cache_file}"

        second = await client.call_tool("get_cluster", {"cluster_id": str(cluster_id)})
        assert not second.is_error
        assert second.data == first_data, "Second identical call diverged from the first"
        assert search_route.call_count == 1
        assert cluster_route.call_count == 1, (
            f"Cluster fetched {cluster_route.call_count} times; "
            "AC-2.5 fetch-once requires exactly 1"
        )
        assert docket_route.call_count == 1, (
            f"Docket fallback fetched {docket_route.call_count} times; "
            "the second cluster call must serve court from cache"
        )
        logger.info(
            f"AC-2.5: {len(results)} stripped hits, cluster {cluster_id} fetched once, "
            f"second call identical ({cache_file.name} cache-served)"
        )


@pytest.mark.asyncio
@respx.mock
async def test_ac_4_7_cache_survives_context_renewal(
    client: Client[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-4.7: after a 'restart' (fresh FileCache over the SAME root), zero HTTP.

    A server restart is proxied by swapping in a brand-new ``FileCache``
    instance pointed at the same cache root (the ``set_cache`` DI seam; the
    cache file is the only persistence layer, so a fresh instance reading the
    same root is exactly the post-restart state). Zero HTTP on the
    post-restart pass is asserted two ways: respx route counts unchanged, and
    a spy on the designated raw fetch helper recording zero invocations.

    Args:
        client: The FastMCP test client fixture.
        monkeypatch: pytest fixture used to install the zero-network spy.

    """
    _lookup, cluster_route, docket_route, opinion_route = _mock_roe_resolution_flow()

    async with client:
        # Seed: resolve a citation and fetch its cluster once (mocked HTTP).
        roe = await client.call_tool(
            "citation_resolve_citation", {"citation": "410 U.S. 113"}
        )
        assert not roe.is_error
        cluster_id = roe.data["cluster"].get("cluster_id")
        assert cluster_id is not None, "Resolved cluster carried no cluster_id"

        first = await client.call_tool("get_cluster", {"cluster_id": str(cluster_id)})
        assert not first.is_error
        first_data = first.data

        # The cache file exists under the expected namespace path (FR-4.1).
        cache_root = get_cache().root
        cache_file = cache_root / "clusters" / f"cluster-{cluster_id}.json"
        assert cache_file.exists(), f"Expected cache file at {cache_file}"

        # "Restart" the server context: brand-new FileCache instance, same root.
        counts_before = (
            cluster_route.call_count,
            docket_route.call_count,
            opinion_route.call_count,
        )
        set_cache(FileCache(root=cache_root))

        # Zero-network spy on the designated raw fetch helper (AC-3.4).
        http_calls = {"count": 0}
        original_fetch = get_module._fetch_resource

        async def counting_fetch(*args: Any, **kwargs: Any) -> dict[str, Any]:
            http_calls["count"] += 1
            return await original_fetch(*args, **kwargs)

        monkeypatch.setattr(get_module, "_fetch_resource", counting_fetch)

        second = await client.call_tool("get_cluster", {"cluster_id": str(cluster_id)})
        assert not second.is_error
        assert second.data == first_data, (
            "Fresh cache context returned different data than the first fetch"
        )
        assert http_calls["count"] == 0, (
            f"Post-restart cluster fetch made {http_calls['count']} HTTP request(s); "
            "AC-4.7 requires zero (cache-served)"
        )
        assert (
            cluster_route.call_count,
            docket_route.call_count,
            opinion_route.call_count,
        ) == counts_before, "Fresh cache context issued HTTP respx saw (AC-4.7: zero)"

        # The fresh instance itself reads the namespace file directly.
        fresh_hit = FileCache(root=cache_root).get("clusters", f"cluster-{cluster_id}")
        assert isinstance(fresh_hit, dict) and fresh_hit, (
            "Fresh FileCache instance failed to serve the cached cluster entry"
        )
        logger.info(
            f"AC-4.7: fresh cache context served cluster {cluster_id} "
            f"identically with {http_calls['count']} HTTP calls"
        )


@pytest.mark.asyncio
@respx.mock
async def test_nfr_1_stripped_under_half_raw_size(client: Client[Any]) -> None:
    """NFR-1: one realistic opinion's stripped JSON < 50% of the raw payload.

    The same opinion ID is fetched twice — once raw
    (``include_all_fields=True``) and once stripped — and serialized sizes are
    compared. The raw fixture is a realistic snake_case opinion detail with
    html variants and metadata junk; the stripped form is the FR-3.1 schema
    (opinionText plus joined cluster metadata).

    Args:
        client: The FastMCP test client fixture.

    """
    raw_opinion_detail = _nfr_opinion_detail()
    opinion_route = respx.get(f"{BASE}/opinions/123456/").mock(
        return_value=httpx.Response(200, json=raw_opinion_detail)
    )
    cluster_route = respx.get(f"{BASE}/clusters/123/").mock(
        return_value=httpx.Response(200, json=ROE_CLUSTER_DETAIL)
    )
    docket_route = respx.get(f"{BASE}/dockets/2759/").mock(
        return_value=httpx.Response(200, json=ROE_DOCKET)
    )

    async with client:
        raw = await client.call_tool(
            "get_opinion", {"opinion_id": "123456", "include_all_fields": True}
        )
        assert not raw.is_error
        stripped = await client.call_tool("get_opinion", {"opinion_id": "123456"})
        assert not stripped.is_error

        # Both calls shared the cached opinion payload: exactly one fetch each.
        assert opinion_route.call_count == 1
        assert cluster_route.call_count == 1  # only the stripped call joins the cluster
        assert docket_route.call_count == 1  # court fallback via cached docket (R1)

        # Raw really is raw (junk fields intact); stripped really is allowlisted.
        assert "plain_text" in raw.data and "html_with_citations" in raw.data
        assert "views" in raw.data and "sha1" in raw.data
        assert set(stripped.data.keys()) <= {
            "caseName",
            "citations",
            "court",
            "dateFiled",
            "opinionText",
            "cluster_id",
            "docket_id",
        }
        assert stripped.data["opinionText"] == raw_opinion_detail["plain_text"]

        raw_len = len(json.dumps(raw.data))
        stripped_len = len(json.dumps(stripped.data))
        assert raw_len > 0
        ratio = stripped_len / raw_len
        assert ratio < 0.5, (
            f"Stripped opinion payload is {stripped_len}/{raw_len} chars "
            f"(ratio {ratio:.3f}); NFR-1 requires < 0.5"
        )
        logger.info(f"NFR-1: stripped opinion payload is {ratio:.1%} of raw size")