"""Get tools for CourtListener MCP server.

Case-law tools (``opinion``/``cluster``/``docket``) are cache-backed (FR-4,
ADR-4) and return stripped FR-3.1 field sets by default, with an
``include_all_fields`` opt-out returning the raw payload (D4). ``audio``,
``person`` and ``court`` are unchanged (Non-goals).
"""

from typing import Annotated, Any

import httpx
from fastmcp import Context, FastMCP
from pydantic import Field

from app import fields
from app.cache import get_cache
from app.config import config, get_auth_headers, get_http_client

get_server: FastMCP[Any] = FastMCP(
    name="CourtListener Get Server",
    instructions="Retrieval server for CourtListener legal database providing direct access to specific records by ID. "
    "This server enables fetching individual records including: court opinions, opinion clusters, court information, "
    "dockets, oral argument audio recordings, and judge/legal professional profiles. "
    "Each tool requires the specific ID of the record to retrieve and returns detailed information about that record. "
    "Use this server when you have a specific ID and need complete details about a particular legal entity. "
    "Case-law tools (opinion/cluster/docket) return a compact allowlisted field set by default and are cached by ID; "
    "pass include_all_fields=true to see every API field.",
)


async def _fetch_resource(
    ctx: Context,
    resource_type: str,
    resource_id: str,
    endpoint: str,
) -> dict[str, Any]:
    """Fetch a resource by ID from the CourtListener API (raw JSON).

    Designated raw fetch helper (AC-3.4): ``response.json()`` lives only here
    (and in the search helper), never in tool return paths.

    Args:
        ctx: The FastMCP context for logging and accessing shared resources.
        resource_type: Human-readable name of the resource (for logging).
        resource_id: The ID of the resource to retrieve.
        endpoint: The API endpoint path (e.g., 'opinions', 'dockets').

    Returns:
        dict: The resource data as returned by the CourtListener API.

    Raises:
        ValueError: If the COURT_LISTENER_API_KEY is not found in environment variables.
        httpx.HTTPStatusError: If the API request fails.

    """
    await ctx.info(f"Getting {resource_type} with ID: {resource_id}")

    headers = get_auth_headers()

    try:
        async with get_http_client(ctx) as http_client:
            response = await http_client.get(
                f"{config.courtlistener_base_url}{endpoint}/{resource_id}/",
                headers=headers,
            )
            response.raise_for_status()
            await ctx.info(f"Successfully retrieved {resource_type} {resource_id}")
            return response.json()

    except httpx.HTTPStatusError as e:
        await ctx.error(f"HTTP error getting {resource_type}: {e}")
        raise
    except Exception as e:
        await ctx.error(f"Error getting {resource_type}: {e}")
        raise


async def cached_fetch(
    ctx: Context,
    cache_ns: str,
    cache_key: str,
    resource_type: str,
    resource_id: str,
) -> dict[str, Any]:
    """Cache-checked fetch for detail endpoints: hit → cached; miss → HTTP → put.

    ``cache_ns`` doubles as the API endpoint (opinions/clusters/dockets), so a
    cache write is skipped on HTTP failures (they raise inside _fetch_resource).

    """
    cache = get_cache()
    cached = cache.get(cache_ns, cache_key)
    if cached is not None:
        await ctx.info(f"Cache hit ({cache_ns}/{cache_key})")
        return cached

    raw = await _fetch_resource(ctx, resource_type, resource_id, cache_ns)
    try:
        cache.put(cache_ns, cache_key, raw)
    except OSError as e:  # unwritable cache dir must not fail the tool (NFR-6)
        await ctx.warning(f"Cache write failed ({cache_ns}/{cache_key}): {e}")
    return raw


async def _apply_court_fallback(ctx: Context, stripped: dict[str, Any]) -> dict[str, Any]:
    """ADR-3 court rule for get tools (tasks.md T4): court via cached docket
    fetch only when the cluster detail omitted it (risk R1). Best-effort — a
    failed enrichment never fails the tool call.

    """
    if "court" in stripped:
        return stripped
    docket_id = stripped.get("docket_id")
    if docket_id is None:
        return stripped
    try:
        raw_docket = await cached_fetch(
            ctx, "dockets", f"docket-{docket_id}", "docket", str(docket_id)
        )
        court = fields.court_from_docket(raw_docket)
        if court is not None:
            stripped["court"] = court
    except Exception as e:  # noqa: BLE001 - enrichment is best-effort (NFR-6)
        await ctx.warning(f"Court fallback via docket {docket_id} failed: {e}")
    return stripped


_INCLUDE_ALL_FIELDS_DESC = "Return the full raw API payload instead of the compact stripped form"


@get_server.tool()
async def opinion(
    opinion_id: Annotated[str, Field(description="The opinion ID to retrieve")],
    ctx: Context,
    include_all_fields: Annotated[bool, Field(description=_INCLUDE_ALL_FIELDS_DESC)] = False,
) -> dict[str, Any]:
    """Get a specific court opinion by ID from CourtListener (cached, compact).

    Compact form: opinionText plus the joined cluster's metadata (caseName,
    citations, court, dateFiled, cluster_id, docket_id). Repeat calls with the
    same ID are served from cache with zero HTTP requests.

    Args:
        opinion_id: The opinion ID to retrieve.
        ctx: The FastMCP context.
        include_all_fields: True returns the raw API payload (D4 opt-out).

    """
    raw_opinion = await cached_fetch(
        ctx, "opinions", f"opinion-{opinion_id}", "opinion", opinion_id
    )
    if include_all_fields:
        return raw_opinion

    cluster_id = fields.extract_cluster_id(raw_opinion)
    if cluster_id is None:
        return fields.strip_resource("opinion", raw_opinion)

    raw_cluster = await cached_fetch(
        ctx, "clusters", f"cluster-{cluster_id}", "opinion cluster", cluster_id
    )
    merged = {**raw_cluster, **raw_opinion}
    return await _apply_court_fallback(ctx, fields.strip_resource("opinion", merged))


@get_server.tool()
async def docket(
    docket_id: Annotated[str, Field(description="The docket ID to retrieve")],
    ctx: Context,
    include_all_fields: Annotated[
        bool, Field(description=_INCLUDE_ALL_FIELDS_DESC)
    ] = False,
) -> dict[str, Any]:
    """Get a specific court docket by ID from CourtListener (cached, compact)."""
    raw = await cached_fetch(ctx, "dockets", f"docket-{docket_id}", "docket", docket_id)
    if include_all_fields:
        return raw
    return fields.strip_resource("docket", raw)


@get_server.tool()
async def audio(
    audio_id: Annotated[str, Field(description="The audio recording ID to retrieve")],
    ctx: Context,
) -> dict[str, Any]:
    """Get oral argument audio information by ID from CourtListener."""
    return await _fetch_resource(ctx, "audio", audio_id, "audio")


@get_server.tool()
async def cluster(
    cluster_id: Annotated[str, Field(description="The opinion cluster ID to retrieve")],
    ctx: Context,
    include_all_fields: Annotated[
        bool, Field(description=_INCLUDE_ALL_FIELDS_DESC)
    ] = False,
) -> dict[str, Any]:
    """Get an opinion cluster by ID from CourtListener (cached, compact).

    Compact form: caseName, citations, court (via one cached docket fetch when
    the cluster detail omits it), dateFiled, cluster_id, docket_id.

    """
    raw = await cached_fetch(ctx, "clusters", f"cluster-{cluster_id}", "cluster", cluster_id)
    if include_all_fields:
        return raw
    return await _apply_court_fallback(ctx, fields.strip_resource("cluster", raw))


@get_server.tool()
async def person(
    person_id: Annotated[str, Field(description="The person (judge) ID to retrieve")],
    ctx: Context,
) -> dict[str, Any]:
    """Get judge or legal professional information by ID from CourtListener."""
    return await _fetch_resource(ctx, "person", person_id, "people")


@get_server.tool()
async def court(
    court_id: Annotated[
        str, Field(description="The court ID to retrieve (e.g., 'scotus', 'ca9')")
    ],
    ctx: Context,
) -> dict[str, Any]:
    """Get court information by ID from CourtListener."""
    return await _fetch_resource(ctx, "court", court_id, "courts")
