"""Search tools for CourtListener MCP server.

Case-law searches (``opinions``/``dockets``/``dockets_with_documents``/
``recap_documents``) send ``highlight=on`` (FR-2.3), strip each hit to the
FR-2.1 allowlist and return a ``{count, results}`` envelope (ADR-5). ``audio``
and ``people`` searches are unchanged raw pass-throughs (Non-goals).
"""

from typing import Annotated, Any

import httpx
from fastmcp import Context, FastMCP
from pydantic import Field

from app import fields
from app.config import config, get_auth_headers, get_http_client
from app.query import parse_natural_query

# Search types that support snippets → send highlight=on (FR-2.3).
_SNIPPET_TYPES: frozenset[str] = frozenset({"o", "r", "rd"})
# Search types whose hits are stripped to the FR-2.1 allowlist; other types
# (oa, p, ...) stay raw pass-through (Non-goals).
_STRIPPED_TYPES: frozenset[str] = frozenset({"o", "d", "r", "rd"})

# Create the search server
search_server: FastMCP[Any] = FastMCP(
    name="CourtListener Search Server",
    instructions="Search server for CourtListener legal database providing comprehensive search capabilities. "
    "This server enables searching across different types of legal content including: "
    "court opinions and cases, oral argument audio recordings, federal dockets from PACER, "
    "RECAP filing documents, and judges/legal professionals. "
    "Search parameters include date ranges, court filters, case names, judge names, and full-text queries. "
    "Results are returned with detailed metadata and can be sorted by relevance or date.",
)


async def _search_courtlistener(
    ctx: Context,
    resource_type: str,
    search_type: str,
    q: str,
    order_by: str,
    limit: int,
    filters: dict[str, Any],
) -> dict[str, Any]:
    """Execute a search against the CourtListener API.

    Args:
        ctx: The FastMCP context for logging and accessing shared resources.
        resource_type: Human-readable name of the resource type (for logging).
        search_type: The CourtListener V4 API type parameter (e.g., 'o', 'd', 'p').
        q: The search query string.
        order_by: Sort order for results.
        limit: Maximum number of results to return.
        filters: Dictionary of optional filter parameters.

    Returns:
        dict: The search results as returned by the CourtListener API.

    Raises:
        ValueError: If COURT_LISTENER_API_KEY is not found in environment variables.
        httpx.HTTPStatusError: If the API request fails.

    """
    await ctx.info(f"Searching {resource_type} with query: {q}")

    headers = get_auth_headers()

    params: dict[str, str | int] = {
        "q": q,
        "order_by": order_by,
        "type": search_type,
    }

    # Snippet-capable types get highlighted snippets (FR-2.3); dockets (d) not.
    if search_type in _SNIPPET_TYPES:
        params["highlight"] = "on"

    # Add limit (V4 uses 'hit' instead of 'limit')
    if limit:
        params["hit"] = limit

    # Add optional filters (only non-empty/non-zero values)
    for key, value in filters.items():
        if value:
            params[key] = value

    try:
        async with get_http_client(ctx) as http_client:
            response = await http_client.get(
                f"{config.courtlistener_base_url}search/",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        if search_type in _STRIPPED_TYPES and isinstance(data, dict):
            hits = [hit for hit in (data.get("results") or []) if isinstance(hit, dict)]
            stripped_results = [fields.strip_search_hit(hit, search_type) for hit in hits]
            await ctx.info(f"Found {data.get('count', 0)} {resource_type}")
            return {
                "count": data.get("count", len(hits)),
                "results": stripped_results,
            }

        await ctx.info(f"Found {data.get('count', 0)} {resource_type}")
        return data

    except httpx.HTTPStatusError as e:
        await ctx.error(f"HTTP error: {e}")
        raise
    except Exception as e:
        await ctx.error(f"Search error: {e}")
        raise


@search_server.tool()
async def opinions(
    q: Annotated[str, Field(description="Search query for full text of opinions")],
    ctx: Context,
    court: Annotated[
        str, Field(description="Court ID filter (e.g., 'scotus', 'ca9')")
    ] = "",
    case_name: Annotated[str, Field(description="Filter by case name")] = "",
    judge: Annotated[str, Field(description="Filter by judge name")] = "",
    filed_after: Annotated[
        str, Field(description="Only show opinions filed after this date (YYYY-MM-DD)")
    ] = "",
    filed_before: Annotated[
        str, Field(description="Only show opinions filed before this date (YYYY-MM-DD)")
    ] = "",
    cited_gt: Annotated[
        int, Field(description="Minimum number of times opinion has been cited", ge=0)
    ] = 0,
    cited_lt: Annotated[
        int, Field(description="Maximum number of times opinion has been cited", ge=0)
    ] = 0,
    order_by: Annotated[
        str,
        Field(description="Sort by 'score desc', 'dateFiled desc', or 'dateFiled asc'"),
    ] = "score desc",
    limit: Annotated[
        int, Field(description="Maximum results to return (1-50)", ge=1, le=50)
    ] = 10,
) -> dict[str, Any]:
    """Search case law opinion clusters with nested Opinion documents in CourtListener.

    Typed parameters beat stuffing everything into q. Worked examples:
    - q="miranda warning", court="scotus", filed_after="1960-01-01",
      filed_before="1970-12-31", limit=10
    - q="seaman status", judge="Alito", cited_gt=50, limit=10

    Hits are stripped to caseName/citations/court/dateFiled/cluster_id/docket_id
    plus a snippet; use cluster_id with get_cluster/get_opinion for full text.
    """
    return await _search_courtlistener(
        ctx=ctx,
        resource_type="opinions",
        search_type="o",
        q=q,
        order_by=order_by,
        limit=limit,
        filters={
            "court": court,
            "case_name": case_name,
            "judge": judge,
            "filed_after": filed_after,
            "filed_before": filed_before,
            "cited_gt": cited_gt,
            "cited_lt": cited_lt,
        },
    )


@search_server.tool()
async def dockets(
    q: Annotated[str, Field(description="Search query for docket text")],
    ctx: Context,
    court: Annotated[
        str, Field(description="Court ID filter (e.g., 'scotus', 'ca9')")
    ] = "",
    case_name: Annotated[str, Field(description="Filter by case name")] = "",
    docket_number: Annotated[
        str, Field(description="Specific docket number to search for")
    ] = "",
    date_filed_after: Annotated[
        str, Field(description="Filter dockets filed after this date (YYYY-MM-DD)")
    ] = "",
    date_filed_before: Annotated[
        str, Field(description="Filter dockets filed before this date (YYYY-MM-DD)")
    ] = "",
    party_name: Annotated[str, Field(description="Filter by party name")] = "",
    order_by: Annotated[
        str,
        Field(description="Sort by 'score desc', 'dateFiled desc', or 'dateFiled asc'"),
    ] = "score desc",
    limit: Annotated[
        int, Field(description="Maximum results to return (1-50)", ge=1, le=50)
    ] = 10,
) -> dict[str, Any]:
    """Search federal cases (dockets) from PACER in CourtListener.

    Typed parameters beat stuffing everything into q. Worked examples:
    - q="patent infringement", court="cafc", docket_number="23-1234", limit=10
    - q="", case_name="Roe", date_filed_after="1970-01-01",
      date_filed_before="1973-12-31", party_name="Wade"

    Docket hits (type d) carry no snippets; results are stripped to
    caseName/docketNumber/court/dateFiled/docket_id.
    """
    return await _search_courtlistener(
        ctx=ctx,
        resource_type="dockets",
        search_type="d",
        q=q,
        order_by=order_by,
        limit=limit,
        filters={
            "court": court,
            "case_name": case_name,
            "docket_number": docket_number,
            "date_filed_after": date_filed_after,
            "date_filed_before": date_filed_before,
            "party_name": party_name,
        },
    )


@search_server.tool()
async def dockets_with_documents(
    q: Annotated[str, Field(description="Search query for federal cases")],
    ctx: Context,
    court: Annotated[
        str, Field(description="Court ID filter (e.g., 'scotus', 'ca9')")
    ] = "",
    case_name: Annotated[str, Field(description="Filter by case name")] = "",
    docket_number: Annotated[
        str, Field(description="Specific docket number to search for")
    ] = "",
    date_filed_after: Annotated[
        str, Field(description="Filter dockets filed after this date (YYYY-MM-DD)")
    ] = "",
    date_filed_before: Annotated[
        str, Field(description="Filter dockets filed before this date (YYYY-MM-DD)")
    ] = "",
    party_name: Annotated[str, Field(description="Filter by party name")] = "",
    order_by: Annotated[
        str,
        Field(description="Sort by 'score desc', 'dateFiled desc', or 'dateFiled asc'"),
    ] = "score desc",
    limit: Annotated[
        int, Field(description="Maximum results to return (1-50)", ge=1, le=50)
    ] = 10,
) -> dict[str, Any]:
    """Search federal cases (dockets) with up to three nested documents.

    If there are more than three matching documents, the more_docs field will be true.

    Typed parameters beat stuffing everything into q. Worked examples:
    - q="summary judgment", court="ca9", date_filed_after="2022-01-01", limit=10
    - q="", party_name="Acme Corp", docket_number="1:23-cv-00001"

    Hits are stripped (type r) and carry top-level snippets (highlight=on is sent).
    """
    return await _search_courtlistener(
        ctx=ctx,
        resource_type="dockets with documents",
        search_type="r",
        q=q,
        order_by=order_by,
        limit=limit,
        filters={
            "court": court,
            "case_name": case_name,
            "docket_number": docket_number,
            "date_filed_after": date_filed_after,
            "date_filed_before": date_filed_before,
            "party_name": party_name,
        },
    )


@search_server.tool()
async def recap_documents(
    q: Annotated[str, Field(description="Search query for RECAP filing documents")],
    ctx: Context,
    court: Annotated[
        str, Field(description="Court ID filter (e.g., 'scotus', 'ca9')")
    ] = "",
    case_name: Annotated[str, Field(description="Filter by case name")] = "",
    docket_number: Annotated[
        str, Field(description="Specific docket number to search for")
    ] = "",
    document_number: Annotated[
        str, Field(description="Specific document number to search for")
    ] = "",
    attachment_number: Annotated[
        str, Field(description="Specific attachment number to search for")
    ] = "",
    filed_after: Annotated[
        str, Field(description="Filter documents filed after this date (YYYY-MM-DD)")
    ] = "",
    filed_before: Annotated[
        str, Field(description="Filter documents filed before this date (YYYY-MM-DD)")
    ] = "",
    party_name: Annotated[str, Field(description="Filter by party name")] = "",
    order_by: Annotated[
        str,
        Field(description="Sort by 'score desc', 'dateFiled desc', or 'dateFiled asc'"),
    ] = "score desc",
    limit: Annotated[
        int, Field(description="Maximum results to return (1-50)", ge=1, le=50)
    ] = 10,
) -> dict[str, Any]:
    """Search federal filing documents from PACER in the RECAP archive.

    Typed parameters beat stuffing everything into q. Worked examples:
    - q="motion to dismiss", court="nysd", document_number="15",
      filed_after="2023-01-01", limit=10
    - q="", case_name="Roe", attachment_number="2", party_name="Wade"

    Hits are stripped (type rd) and carry top-level snippets (highlight=on is sent).
    """
    return await _search_courtlistener(
        ctx=ctx,
        resource_type="RECAP documents",
        search_type="rd",
        q=q,
        order_by=order_by,
        limit=limit,
        filters={
            "court": court,
            "case_name": case_name,
            "docket_number": docket_number,
            "document_number": document_number,
            "attachment_number": attachment_number,
            "filed_after": filed_after,
            "filed_before": filed_before,
            "party_name": party_name,
        },
    )


@search_server.tool()
async def natural_language(
    query: Annotated[
        str,
        Field(
            description=(
                "Free-text case question, e.g. 'SCOTUS, qualified immunity, after 2015' "
                "or 'ninth circuit \"official capacity\" before 2020'. Interpreted "
                "deterministically: court aliases, date phrases, quoted phrases, "
                "judge/case hints; everything else stays in q."
            )
        ),
    ],
    ctx: Context,
    limit: Annotated[
        int, Field(description="Maximum results to return (1-50)", ge=1, le=50)
    ] = 10,
) -> dict[str, Any]:
    """Search opinions from a natural-language question (FR-5, ADR-6).

    Deterministic (no LLM, no network for the interpretation): maps court
    aliases ("supreme court" → scotus, "ninth circuit" → ca9), date phrases
    ("after 2015" → 2015-01-01), quoted phrases, and judge/case hints onto the
    typed parameters of the opinions search; anything unmapped stays in ``q``.

    Returns ``{interpreted_query, count, results}`` — check
    ``interpreted_query`` to see (and correct) the interpretation.

    Worked examples:
    - query="SCOTUS, qualified immunity, after 2015"
    - query='ninth circuit "official capacity" before 2020'
    - query="judge Kagan, case Miranda, since 2015-06"

    """
    parsed = parse_natural_query(query)
    interpreted = parsed.params()
    await ctx.info(f"Natural-language query interpreted as: {interpreted}")

    stripped_search = await _search_courtlistener(
        ctx=ctx,
        resource_type="opinions",
        search_type="o",
        q=interpreted.get("q", ""),
        order_by="score desc",
        limit=limit,
        filters={
            "court": interpreted.get("court", ""),
            "case_name": interpreted.get("case_name", ""),
            "judge": interpreted.get("judge", ""),
            "filed_after": interpreted.get("filed_after", ""),
            "filed_before": interpreted.get("filed_before", ""),
        },
    )
    return {"interpreted_query": interpreted, **stripped_search}


@search_server.tool()
async def audio(
    q: Annotated[str, Field(description="Search query for oral argument audio")],
    ctx: Context,
    court: Annotated[
        str, Field(description="Court ID filter (e.g., 'scotus', 'ca9')")
    ] = "",
    case_name: Annotated[str, Field(description="Filter by case name")] = "",
    judge: Annotated[str, Field(description="Filter by judge name")] = "",
    argued_after: Annotated[
        str, Field(description="Filter arguments after this date (YYYY-MM-DD)")
    ] = "",
    argued_before: Annotated[
        str, Field(description="Filter arguments before this date (YYYY-MM-DD)")
    ] = "",
    order_by: Annotated[
        str,
        Field(
            description="Sort by 'score desc', 'dateArgued desc', or 'dateArgued asc'"
        ),
    ] = "score desc",
    limit: Annotated[
        int, Field(description="Maximum results to return", ge=1, le=100)
    ] = 20,
) -> dict[str, Any]:
    """Search oral argument audio recordings in CourtListener.

    Typed parameters beat stuffing everything into q. Worked examples:
    - q="qualified immunity", court="scotus", argued_after="2022-10-01", limit=10
    - q="", case_name="Moore", judge="Roberts", argued_before="2023-06-30"
    """
    return await _search_courtlistener(
        ctx=ctx,
        resource_type="audio recordings",
        search_type="oa",
        q=q,
        order_by=order_by,
        limit=limit,
        filters={
            "court": court,
            "case_name": case_name,
            "judge": judge,
            "dateArgued_after": argued_after,
            "dateArgued_before": argued_before,
        },
    )


@search_server.tool()
async def people(
    q: Annotated[
        str, Field(description="Search query for judges and legal professionals")
    ],
    ctx: Context,
    name: Annotated[str, Field(description="Filter by person's name")] = "",
    position_type: Annotated[
        str, Field(description="Filter by position type (e.g., 'jud' for judge)")
    ] = "",
    political_affiliation: Annotated[
        str, Field(description="Filter by political affiliation")
    ] = "",
    school: Annotated[str, Field(description="Filter by school attended")] = "",
    appointed_by: Annotated[
        str, Field(description="Filter by appointing authority")
    ] = "",
    selection_method: Annotated[
        str, Field(description="Filter by selection method")
    ] = "",
    order_by: Annotated[
        str, Field(description="Sort by 'score desc' or 'name asc'")
    ] = "score desc",
    limit: Annotated[
        int, Field(description="Maximum results to return", ge=1, le=100)
    ] = 20,
) -> dict[str, Any]:
    """Search judges and legal professionals in the CourtListener database.

    Typed parameters beat stuffing everything into q. Worked examples:
    - q="Roberts", position_type="jud", limit=10
    - q="", name="Sonia Sotomayor", appointed_by="Obama", school="Princeton"
    """
    return await _search_courtlistener(
        ctx=ctx,
        resource_type="people",
        search_type="p",
        q=q,
        order_by=order_by,
        limit=limit,
        filters={
            "name": name,
            "position_type": position_type,
            "political_affiliation": political_affiliation,
            "school": school,
            "appointed_by": appointed_by,
            "selection_method": selection_method,
        },
    )
