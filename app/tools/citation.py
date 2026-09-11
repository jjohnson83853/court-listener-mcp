"""Citation lookup tools for CourtListener MCP server.

This module provides FastMCP tools for legal citation lookup, parsing, and validation
using the CourtListener API and citeurl library. It includes tools for single and batch
citation lookup, citation format verification, parsing, extraction from text, and
enhanced lookups combining citeurl and CourtListener data.
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

import httpx
from citeurl import Citator, list_cites  # type: ignore[import-untyped]
from citeurl import cite as citeurl_cite
from fastmcp import Context, FastMCP
from loguru import logger
from pydantic import Field

from app import fields
from app.cache import get_cache, slugify_key
from app.config import config, get_auth_headers, get_http_client
from app.tools.get import cached_fetch

# Create the citation server
citation_server: FastMCP[Any] = FastMCP(
    name="CourtListener Citation Server",
    instructions="Citation processing server for legal citation lookup, parsing, validation, and analysis. "
    "This server combines the power of the CourtListener API with the citeurl library to provide comprehensive citation tools. "
    "Available capabilities include: single and batch citation lookup, citation format verification and validation, "
    "citation parsing and normalization, citation extraction from text, and enhanced lookups combining multiple data sources. "
    "Supports various citation formats including U.S. Reporter, Federal Reporter, WestLaw, and state reporter citations. "
    "Use this server for all citation-related tasks including validation, parsing, and data retrieval.",
)


@citation_server.tool()
async def lookup_citation(
    citation: Annotated[
        str,
        Field(
            description="The citation to look up (e.g., '410 U.S. 113', '2023 WL 12345')"
        ),
    ],
    ctx: Context,
) -> dict[str, Any]:
    """Look up a legal citation to find the opinion it references in CourtListener.

    This tool accepts various citation formats including:
    - U.S. Reporter citations (e.g., "410 U.S. 113")
    - Federal Reporter citations (e.g., "123 F.3d 456")
    - WestLaw citations (e.g., "2023 WL 12345")
    - State reporter citations

    Args:
        citation: The citation string to look up.
        ctx: The FastMCP context for logging and accessing shared resources.

    Returns:
        dict[str, Any]: The opinion(s) that match the citation, or an error dict if the lookup fails.

    Raises:
        ValueError: If COURT_LISTENER_API_KEY is not found in environment variables.
        httpx.HTTPStatusError: If the API request fails.

    """
    await ctx.info(f"Looking up citation: {citation}")

    headers = get_auth_headers()

    try:
        async with get_http_client(ctx) as http_client:
            response = await http_client.post(
                f"{config.courtlistener_base_url}citation-lookup/",
                headers=headers,
                data={"text": citation},
            )
            response.raise_for_status()
            data = response.json()

        # Wrap list responses in a dict for MCP compatibility
        if isinstance(data, list):
            result: dict[str, Any] = {
                "citation": citation,
                "count": len(data),
                "results": data,
            }
        else:
            result = data

        await ctx.info(f"Successfully looked up citation: {citation}")
        return result

    except httpx.HTTPStatusError as e:
        await ctx.error(f"HTTP error looking up citation: {e}")
        raise
    except Exception as e:
        await ctx.error(f"Error looking up citation: {e}")
        raise


@citation_server.tool()
async def batch_lookup_citations(
    citations: Annotated[
        list[str],
        Field(
            description="List of citations to look up (max 100)",
            min_length=1,
            max_length=100,
        ),
    ],
    ctx: Context,
) -> dict[str, Any]:
    """Look up multiple legal citations in a single request.

    This is more efficient than making individual requests for each citation.
    Accepts up to 100 citations at once.

    Args:
        citations: List of citation strings to look up (max 100).
        ctx: The FastMCP context for logging and accessing shared resources.

    Returns:
        dict[str, Any]: A dictionary mapping each citation to its corresponding opinion(s).

    Raises:
        ValueError: If COURT_LISTENER_API_KEY is not found in environment variables.
        httpx.HTTPStatusError: If the API request fails.

    """
    await ctx.info(f"Looking up {len(citations)} citations")

    headers = get_auth_headers()

    try:
        # Use POST with form data for the citation text
        # Join all citations into one text block separated by spaces
        citation_text = " ".join(citations)
        async with get_http_client(ctx) as http_client:
            response = await http_client.post(
                f"{config.courtlistener_base_url}citation-lookup/",
                headers=headers,
                data={"text": citation_text},
                timeout=config.courtlistener_timeout * 2,  # Longer timeout for batch requests
            )
            response.raise_for_status()
            data = response.json()

        # Wrap list responses in a dict for MCP compatibility, stripping each
        # entry's candidate clusters to the FR-3.1 schema (D3) while keeping
        # per-citation statuses, messages and counts verbatim.
        if isinstance(data, list):
            stripped_results: list[dict[str, Any]] = []
            for entry in data:
                if isinstance(entry, dict):
                    stripped_entry = dict(entry)
                    stripped_entry["clusters"] = fields.strip_candidates(
                        entry.get("clusters", [])
                    )
                    stripped_results.append(stripped_entry)
                else:
                    stripped_results.append(entry)
            result: dict[str, Any] = {
                "citations_requested": citations,
                "count": len(stripped_results),
                "results": stripped_results,
            }
        else:
            result = data

        await ctx.info(f"Successfully looked up {len(citations)} citations")
        return result

    except httpx.HTTPStatusError as e:
        await ctx.error(f"HTTP error in batch citation lookup: {e}")
        raise
    except Exception as e:
        await ctx.error(f"Error in batch citation lookup: {e}")
        raise


def _normalize_citation_for_lookup(citation: str) -> str:
    """Optional citeurl pass (ADR-3 step 1): canonical text or the raw string."""
    try:
        citator = get_citator()
        parsed = citeurl_cite(citation, broad=True, citator=citator)
    except Exception:  # noqa: BLE001 - citeurl failure sends the raw string (ADR-3)
        return citation
    text = getattr(parsed, "text", None)
    return str(text) if text else citation


def _normalize_status(entry: dict[str, Any]) -> int:
    """Per-citation status as int; unparseable values degrade to 0 (unknown)."""
    status = entry.get("status")
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    if isinstance(status, str) and status.isdigit():
        return int(status)
    return 0


def _sub_opinion_ids(raw_cluster: dict[str, Any]) -> list[str]:
    """Sub-opinion IDs from a cluster detail record (``sub_opinions`` URLs)."""
    ids: list[str] = []
    for ref in raw_cluster.get("sub_opinions") or []:
        resource_id = fields.resource_id_from_ref(ref)
        if resource_id is not None:
            ids.append(resource_id)
    return ids


async def _fetch_opinion_cached(ctx: Context, opinion_id: str) -> dict[str, Any]:
    """Fetch (and cache) an opinion detail record by ID."""
    return await cached_fetch(
        ctx, "opinions", f"opinion-{opinion_id}", "opinion", opinion_id
    )


def _disposition_result(citation: str, status: int, entry: dict[str, Any]) -> dict[str, Any] | None:
    """Map a non-200 per-citation status to its structured result (FR-1.2).

    Returns None when the status permits proceeding (200). No status triggers
    a retry or a search fallback (Non-goals).

    """
    error_message = entry.get("error_message") or ""
    if status == 429:
        return {
            "citation": citation,
            "status": 429,
            "found": False,
            "message": "CourtListener reported 429 (over cap) for this citation; surfaced without retry.",
            "error_message": error_message,
        }
    if status == 404:
        return {
            "citation": citation,
            "status": 404,
            "found": False,
            "message": "Citation is validly formatted but not found in the CourtListener corpus.",
        }
    if status == 400:
        return {
            "citation": citation,
            "status": 400,
            "found": False,
            "message": f"Invalid citation or unknown reporter.{(' ' + error_message) if error_message else ''}",
        }
    if status == 300:
        return {
            "citation": citation,
            "status": 300,
            "found": False,
            "message": "Ambiguous citation: multiple clusters match. Pick one and fetch it by cluster_id.",
            "candidates": fields.strip_candidates(entry.get("clusters") or []),
        }
    if status != 200:
        return {
            "citation": citation,
            "status": status,
            "found": False,
            "message": f"Unexpected citation-lookup status {status}.",
        }
    return None


@citation_server.tool()
async def resolve_citation(
    citation: Annotated[
        str,
        Field(
            description="The citation to resolve exactly (e.g., '410 U.S. 113', '123 F.3d 456')"
        ),
    ],
    ctx: Context,
    all_opinions: Annotated[
        bool,
        Field(
            description="Fetch every sub-opinion's text instead of only the lead opinion"
        ),
    ] = False,
) -> dict[str, Any]:
    """Resolve an exact citation to its matched cluster and the opinion text (FR-1, ADR-3).

    Flow: optional citeurl normalization → cached ``citations/`` mapping →
    ``POST citation-lookup/`` → status dispatch (200/300/404/400/429 — no
    retry, no search fallback) → cached cluster fetch → lead opinion text
    (cached; all sub-opinions with ``all_opinions=true``).

    Returns ``{citation, status, cluster: {caseName, citations, court,
    dateFiled, cluster_id, docket_id}, opinionText}``. On ``300`` the result
    carries stripped ``candidates`` instead of fetching anything. ``court`` may
    require one extra cached docket fetch when the cluster omits it. Text
    falls back plain_text → html_with_citations → html; if all are missing the
    result carries a ``text_unavailable`` note instead of failing.

    """
    await ctx.info(f"Resolving citation: {citation}")
    headers = get_auth_headers()

    cache = get_cache()
    slug = slugify_key(citation)

    cached_entry = cache.get("citations", slug)
    cluster_id = (
        fields.resource_id_from_ref(cached_entry.get("cluster_id"))
        if isinstance(cached_entry, dict)
        else None
    )
    if cluster_id is not None:
        await ctx.info(f"Cache hit (citations/{slug}) → cluster {cluster_id}")

    if cluster_id is None:
        normalized = _normalize_citation_for_lookup(citation)
        async with get_http_client(ctx) as http_client:
            response = await http_client.post(
                f"{config.courtlistener_base_url}citation-lookup/",
                headers=headers,
                data={"text": normalized},
            )
            response.raise_for_status()
            data = response.json()

        entries = data if isinstance(data, list) else [data]
        entry = entries[0] if entries and isinstance(entries[0], dict) else {}
        if not entry:
            # Empty/missing lookup array: treat as valid-format-not-found (M4)
            # instead of a misleading "status 0".
            await ctx.info(f"Citation {citation} → no lookup entry (treated as not found)")
            return {
                "citation": citation,
                "status": 404,
                "found": False,
                "message": "Citation-lookup returned no entry for this citation (not found in corpus).",
            }
        status = _normalize_status(entry)
        disposition = _disposition_result(citation, status, entry)
        if disposition is not None:
            await ctx.info(f"Citation {citation} → status {status} (no follow-up fetch)")
            return disposition

        clusters = entry.get("clusters") or []
        first_cluster = clusters[0] if clusters else None
        cluster_id = (
            fields.resource_id_from_ref(first_cluster.get("id"))
            if isinstance(first_cluster, dict)
            else None
        )
        if cluster_id is None:
            return {
                "citation": citation,
                "status": status,
                "found": False,
                "message": "Citation-lookup returned a match without a usable cluster id.",
            }
        try:
            cache.put("citations", slug, {"cluster_id": int(cluster_id)})
        except OSError as e:  # unwritable cache dir must not fail the tool (NFR-6)
            await ctx.warning(f"Cache write failed (citations/{slug}): {e}")

    raw_cluster = await cached_fetch(
        ctx, "clusters", f"cluster-{cluster_id}", "cluster", cluster_id
    )
    stripped = fields.strip_resource("cluster", raw_cluster)

    if "court" not in stripped:
        docket_id = stripped.get("docket_id")
        if docket_id is not None:
            raw_docket = await cached_fetch(
                ctx, "dockets", f"docket-{docket_id}", "docket", str(docket_id)
            )
            court = fields.court_from_docket(raw_docket)
            if court is not None:
                stripped["court"] = court

    sub_opinion_ids = _sub_opinion_ids(raw_cluster)
    opinion_text: str | None = None
    collected_opinions: list[dict[str, Any]] = []

    lead_opinion_id = sub_opinion_ids[0] if sub_opinion_ids else None
    if lead_opinion_id is not None:
        lead_opinion_raw = await _fetch_opinion_cached(ctx, lead_opinion_id)
        opinion_text = fields.extract_opinion_text(lead_opinion_raw)

    if all_opinions:
        for opinion_id in sub_opinion_ids:
            raw_opinion = await _fetch_opinion_cached(ctx, opinion_id)
            collected_opinions.append(
                {
                    "opinion_id": int(opinion_id) if opinion_id.isdigit() else opinion_id,
                    "type": raw_opinion.get("type"),
                    "text": fields.extract_opinion_text(raw_opinion),
                }
            )

    result: dict[str, Any] = {
        "citation": citation,
        "status": 200,
        "cluster": stripped,
        "opinionText": opinion_text,
    }
    if all_opinions:
        result["opinions"] = collected_opinions
    if opinion_text is None and sub_opinion_ids:
        result["note"] = "text_unavailable"
        result["sub_opinions"] = [
            int(oid) if oid.isdigit() else oid for oid in sub_opinion_ids
        ]

    await ctx.info(f"Resolved citation {citation} → cluster {cluster_id}")
    return result


@citation_server.tool()
async def verify_citation_format(
    citation: Annotated[
        str,
        Field(description="The citation to verify"),
    ],
    ctx: Context,
) -> dict[str, Any]:
    """Verify if a citation string is in a valid format using citeurl's advanced parsing.

    This tool performs validation using citeurl's comprehensive citation templates
    to check if a citation appears to be in a recognized legal citation format.
    This is much more accurate than simple regex matching.

    Returns information about the citation format and any detected issues.

    Args:
        citation: The citation string to verify.
        ctx: The FastMCP context for logging.

    Returns:
        dict[str, str | bool | list[str] | None]: A dictionary containing validation results with:
            - valid: Whether the citation is in a valid format
            - format: The recognized citation format type (if valid)
            - template: The citation template matched (if valid)
            - issues: List of any validation issues found
            - citation: The original citation string

    """
    await ctx.info(f"Verifying citation format: {citation}")

    citation_stripped = citation.strip()

    # Check if citation is empty
    if not citation_stripped:
        return {
            "valid": False,
            "format": None,
            "template": None,
            "issues": ["Citation is empty"],
            "citation": citation,
        }

    try:
        citator = get_citator()

        # Try broad matching first
        parsed_broad = citeurl_cite(citation_stripped, broad=True, citator=citator)
        # Try strict matching
        parsed_strict = citeurl_cite(citation_stripped, broad=False, citator=citator)

        if parsed_strict:
            # Citation is valid in strict mode
            result = {
                "valid": True,
                "format": "Recognized legal citation",
                "template": str(parsed_strict.template),
                "matching_mode": "strict",
                "citation": citation,
                "normalized": parsed_strict.text,
                "tokens": parsed_strict.tokens,
                "issues": [],
            }
        elif parsed_broad:
            # Citation is valid only in broad mode
            result = {
                "valid": True,
                "format": "Recognized legal citation (broad matching)",
                "template": str(parsed_broad.template),
                "matching_mode": "broad",
                "citation": citation,
                "normalized": parsed_broad.text,
                "tokens": parsed_broad.tokens,
                "issues": [
                    "Citation recognized only with broad matching - may be informal format"
                ],
            }
        else:
            # Citation not recognized by citeurl
            result = {
                "valid": False,
                "format": None,
                "template": None,
                "matching_mode": None,
                "citation": citation,
                "normalized": citation_stripped,
                "issues": [
                    "Citation does not match any recognized legal citation format in citeurl's templates",
                    "Consider checking the citation format against standard legal citation styles (Bluebook, etc.)",
                ],
            }

    except Exception as e:  # noqa: BLE001 - citeurl failure degrades to basic-pattern fallback
        # Fallback to basic validation if citeurl fails
        logger.warning(
            f"citeurl verification failed, falling back to basic patterns: {e}"
        )

        # Basic patterns for fallback
        basic_patterns = {
            "U.S. Reporter": r"^\d+\s+U\.S\.\s+\d+",
            "Federal Reporter": r"^\d+\s+F\.(2d|3d|4th)?\s+\d+",
            "Federal Supplement": r"^\d+\s+F\.\s*Supp\.(2d|3d)?\s+\d+",
            "State Reporter": r"^\d+\s+[A-Z][a-z]+\.(\s*(2d|3d|4th))?\s+\d+",
        }

        matched_format = None
        for format_name, pattern in basic_patterns.items():
            if re.match(pattern, citation_stripped, re.IGNORECASE):
                matched_format = format_name
                break

        result = {
            "valid": matched_format is not None,
            "format": matched_format,
            "template": "Basic regex fallback",
            "matching_mode": "fallback",
            "citation": citation,
            "normalized": citation_stripped,
            "issues": ["Verified using basic patterns only - citeurl parsing failed"]
            if matched_format
            else [
                "Citation not recognized by basic patterns",
                "citeurl parsing also failed",
                f"Error: {e}",
            ],
        }

    await ctx.info(f"Citation format verification complete: {result['valid']}")
    return result


def get_citator() -> Citator:
    """Get or create the citeurl citator instance with custom citation templates.

    Returns:
        Citator: The singleton citeurl Citator instance with custom citation support.

    """
    return _get_citator_singleton()


@lru_cache(maxsize=1)
def _get_citator_singleton() -> Citator:
    """Get or create the citeurl citator instance with custom citation templates.

    Returns:
        Citator: The singleton citeurl Citator instance with custom citation support.

    """
    template_path = Path(__file__).parent / "custom_citation_templates.yaml"
    citator = Citator(yaml_paths=[str(template_path)])
    logger.info(f"Created citator with custom citation templates from {template_path}")
    return citator


@citation_server.tool()
async def parse_citation_with_citeurl(
    citation: Annotated[
        str,
        Field(
            description="The citation to parse (e.g., '410 U.S. 113', '42 USC § 1988')"
        ),
    ],
    ctx: Context,
    broad: Annotated[
        bool,
        Field(description="Use broad matching for more flexible parsing", default=True),
    ] = True,
) -> dict[str, Any]:
    """Parse a legal citation using citeurl's advanced citation recognition.

    This tool uses the citeurl library to parse legal citations and extract
    structured information including tokens, normalized format, and URL generation.

    Returns detailed information about the citation including:
    - Recognized citation format and source
    - Extracted tokens (volume, reporter, page, etc.)
    - Generated URL if available
    - Normalized citation text

    Args:
        citation: The citation string to parse.
        ctx: The FastMCP context for logging.
        broad: Whether to use broad matching for flexible parsing.

    Returns:
        dict[str, str | dict | None]: A dictionary containing the parsed citation data,
            including success status, original citation, and detailed parsing results.

    """
    await ctx.info(f"Parsing citation with citeurl: {citation}")

    try:
        citator = get_citator()
        parsed_citation = citeurl_cite(citation, broad=broad, citator=citator)

        if not parsed_citation:
            return {
                "success": False,
                "error": "Citation not recognized by citeurl",
                "citation": citation,
                "suggestion": "Try using a more standard citation format",
            }

        result = {
            "success": True,
            "citation": citation,
            "parsed": {
                "text": parsed_citation.text,
                "tokens": parsed_citation.tokens,
                "template": str(parsed_citation.template),
                "URL": getattr(parsed_citation, "URL", None),
                "canonical_name": getattr(parsed_citation, "name", None),
            },
        }

        await ctx.info(f"Successfully parsed citation: {parsed_citation.text}")
        return result

    except Exception as e:
        await ctx.error(f"Error parsing citation with citeurl: {e}")
        raise


@citation_server.tool()
async def extract_citations_from_text(
    text: Annotated[
        str,
        Field(description="Text containing legal citations to extract"),
    ],
    ctx: Context,
) -> dict[str, Any]:
    """Extract all legal citations from a block of text using citeurl.

    This tool finds and parses all legal citations within a given text,
    including both long-form and short-form citations (like 'id.' references).

    Args:
        text: The text containing legal citations to extract.
        ctx: The FastMCP context for logging.

    Returns:
        dict[str, list | int]: A dictionary containing:
            - total_citations: Number of citations found
            - citations: List of parsed citation information
            - text_length: Length of the input text
            - error (optional): Error message if extraction failed

    """
    await ctx.info(f"Extracting citations from text ({len(text)} characters)")

    try:
        citator = get_citator()
        citations = list_cites(text, citator=citator)

        parsed_citations = []
        for citation in citations:
            citation_info = {
                "text": citation.text,
                "tokens": citation.tokens,
                "template": str(citation.template),
                "URL": getattr(citation, "URL", None),
                "canonical_name": getattr(citation, "name", None),
            }
            parsed_citations.append(citation_info)

        result = {
            "total_citations": len(citations),
            "citations": parsed_citations,
            "text_length": len(text),
        }

        await ctx.info(f"Found {len(citations)} citations in text")
        return result

    except Exception as e:
        await ctx.error(f"Error extracting citations from text: {e}")
        raise


@citation_server.tool()
async def enhanced_citation_lookup(
    citation: Annotated[
        str,
        Field(description="The citation to look up and analyze"),
    ],
    ctx: Context,
    include_courtlistener: Annotated[
        bool,
        Field(
            description="Whether to also perform CourtListener API lookup", default=True
        ),
    ] = True,
) -> dict[str, Any]:
    """Enhanced citation lookup combining citeurl parsing with CourtListener data.

    This tool first uses citeurl to parse and validate the citation format,
    then optionally queries the CourtListener API for additional case information.

    Returns:
        dict: Comprehensive citation information from both sources, containing:
            - citation: The original citation string
            - citeurl_analysis: Parsing results from citeurl
            - courtlistener_data: Lookup results from CourtListener API
            - combined_info: Summary of available information from both sources

    Args:
        citation: The citation string to look up and analyze.
        ctx: The FastMCP context for logging and accessing shared resources.
        include_courtlistener: Whether to include CourtListener API lookup.

    Returns:
        dict[str, dict | str | bool]: A dictionary containing the enhanced citation information.

    """
    await ctx.info(f"Enhanced lookup for citation: {citation}")

    result = {
        "citation": citation,
        "citeurl_analysis": {},
        "courtlistener_data": {},
        "combined_info": {},
    }

    # First, parse with citeurl
    try:
        citator = get_citator()
        parsed = citeurl_cite(citation, broad=True, citator=citator)

        if parsed:
            result["citeurl_analysis"] = {
                "success": True,
                "text": parsed.text,
                "tokens": parsed.tokens,
                "template": str(parsed.template),
                "URL": getattr(parsed, "URL", None),
                "canonical_name": getattr(parsed, "name", None),
            }
        else:
            result["citeurl_analysis"] = {
                "success": False,
                "error": "Citation not recognized by citeurl",
            }
    except Exception as e:  # noqa: BLE001 - parse errors become structured analysis results
        result["citeurl_analysis"] = {
            "success": False,
            "error": f"citeurl parsing error: {e}",
        }

    # Then, lookup in CourtListener if requested and API key available
    if include_courtlistener:
        try:
            headers = get_auth_headers()
            async with get_http_client(ctx) as http_client:
                response = await http_client.post(
                    f"{config.courtlistener_base_url}citation-lookup/",
                    headers=headers,
                    data={"text": citation},
                )
                if response.status_code == 200:
                    result["courtlistener_data"] = {
                        "success": True,
                        "data": response.json(),
                    }
                else:
                    result["courtlistener_data"] = {
                        "success": False,
                        "error": f"HTTP {response.status_code}: {response.text}",
                    }
        except ValueError:
            result["courtlistener_data"] = {
                "success": False,
                "error": "COURT_LISTENER_API_KEY not found",
            }
        except Exception as e:  # noqa: BLE001 - API errors surface as structured tool data
            result["courtlistener_data"] = {
                "success": False,
                "error": f"CourtListener API error: {e}",
            }

    # Combine information
    citeurl_analysis = result.get("citeurl_analysis", {})
    courtlistener_data = result.get("courtlistener_data", {})
    if (
        isinstance(citeurl_analysis, dict)
        and citeurl_analysis.get("success")
        and isinstance(courtlistener_data, dict)
        and courtlistener_data.get("success")
    ):
        result["combined_info"] = {
            "has_both_sources": True,
            "citeurl_url": citeurl_analysis.get("URL"),
            "canonical_citation": citeurl_analysis.get("canonical_name"),
            "tokens": citeurl_analysis.get("tokens"),
            "courtlistener_matches": len(courtlistener_data.get("data", [])),
        }
    else:
        available_sources = []
        if isinstance(citeurl_analysis, dict) and citeurl_analysis.get("success"):
            available_sources.append("citeurl")
        if isinstance(courtlistener_data, dict) and courtlistener_data.get("success"):
            available_sources.append("courtlistener")
        result["combined_info"] = {
            "has_both_sources": False,
            "available_sources": available_sources,
        }

    await ctx.info(f"Enhanced lookup complete for: {citation}")
    return result
