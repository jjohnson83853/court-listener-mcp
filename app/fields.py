"""Field allowlists and stripping helpers for token efficiency (FR-3, ADR-1).

Every tool boundary that returns case-law data to the client passes its payload
through one of the ``strip_*`` helpers here exactly once. Stripping is
allowlist-based and deliberately forgiving (NFR-6):

- Missing allowlisted fields are **omitted**, never null-padded, never fatal.
- Unknown fields in the API response are silently dropped.
- Casing differences between the API surfaces (detail endpoints return
  snake_case, search hits return camelCase — design finding 1.2) are
  normalized away by a data-driven source map, so output keys are stable.

Snippet extraction (design risk R2): for ``o``-type search hits the snippet is
nested in ``sub_opinions``; we take the lead (first) sub-opinion's snippet.
For ``r``/``rd``-type hits the snippet is top-level.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

# URL (or bare number) → numeric ID extraction, e.g.
# ".../api/rest/v4/dockets/12345/" → 12345
_URL_ID_RE = re.compile(r"/(\d+)/?$")


class _HTMLTextExtractor(HTMLParser):
    """Collect visible text from HTML, stdlib-only (ADR-3 text fallback)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self._parts.append(text)

    def get_text(self) -> str:
        return " ".join(self._parts)


def _html_to_text(html: str) -> str:
    """Strip HTML tags down to visible text."""
    extractor = _HTMLTextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:  # noqa: BLE001 - malformed HTML must not break stripping (NFR-6)
        # Extremely defensive: fall back to a crude tag strip.
        return re.sub(r"<[^>]+>", " ", html)
    return extractor.get_text()


def _value_to_id(value: Any) -> int | None:
    """Coerce an ID-ish value (int, digit string, or resource URL) to int."""
    if isinstance(value, bool):  # bool is an int subclass; not an ID
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        value = value.strip()
        if value.isdigit():
            return int(value)
        match = _URL_ID_RE.search(value)
        if match:
            return int(match.group(1))
    return None


def resource_id_from_ref(value: Any) -> str | None:
    """Extract a resource ID string from an int, digit string, or resource URL.

    Handles the URL forms CourtListener uses for FKs, e.g.
    ``.../api/rest/v4/clusters/123/`` → ``"123"``.

    """
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return text
        match = _URL_ID_RE.search(text)
        if match:
            return match.group(1)
    return None


def extract_cluster_id(data: dict[str, Any]) -> str | None:
    """Cluster ID from a lookup/detail/merged record (``cluster_id`` or ``cluster`` URL)."""
    ref = data.get("cluster_id") or data.get("cluster")
    return resource_id_from_ref(ref)


def _normalize_court(value: Any) -> str | None:
    """Normalize a court value: FK URL (``.../courts/scotus/``) → id (``scotus``)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().rstrip("/")
    if "/" in text:
        return text.rsplit("/", 1)[-1]
    return text


def court_from_docket(raw_docket: dict[str, Any]) -> str | None:
    """Court id from a docket detail record (``court`` FK URL or plain id)."""
    return _normalize_court(_pick(raw_docket, ("court", "court_id")))


def _pick(data: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    """Return the first present, non-None value among candidate keys."""
    for key in candidates:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _pick_id(data: dict[str, Any], candidates: tuple[str, ...]) -> int | None:
    """Pick the first candidate key whose value looks like an ID/URL."""
    return _value_to_id(_pick(data, candidates))


def _pick_text(data: dict[str, Any], candidates: tuple[str, ...]) -> str | None:
    """Pick the first candidate whose value is a non-empty string."""
    value = _pick(data, candidates)
    if isinstance(value, str) and value.strip():
        return value
    return None


# --------------------------------------------------------------------------
# Per-kind source maps: normalized output key -> candidate input keys.
# First present candidate wins; snake_case candidates come first because the
# detail endpoints are the richest surfaces (design finding 1.2).
# --------------------------------------------------------------------------

_CLUSTER_SOURCES: dict[str, tuple[str, ...]] = {
    "caseName": ("case_name", "caseName"),
    "citations": ("citations", "citation"),
    "court": ("court", "court_id"),
    "dateFiled": ("date_filed", "dateFiled"),
    "cluster_id": ("cluster_id", "id"),
    "docket_id": ("docket_id", "docket"),  # "docket" is a URL on cluster detail
}

_OPINION_SOURCES: dict[str, tuple[str, ...]] = {
    "caseName": ("case_name", "caseName"),
    "citations": ("citations", "citation"),
    "court": ("court", "court_id"),
    "dateFiled": ("date_filed", "dateFiled"),
    "cluster_id": ("cluster_id", "cluster"),  # "cluster" is a URL on opinion detail
    "docket_id": ("docket_id", "docket"),  # "docket" is a URL on the joined cluster
}

_DOCKET_SOURCES: dict[str, tuple[str, ...]] = {
    "caseName": ("case_name", "caseName"),
    "docketNumber": ("docket_number", "docketNumber"),
    "court": ("court", "court_id"),
    "dateFiled": ("date_filed", "dateFiled"),
    "docket_id": ("docket_id", "id"),
}

# Citation-lookup cluster objects (FR-3.1 candidates; finding 1.2 — no court)
_CANDIDATE_SOURCES: dict[str, tuple[str, ...]] = {
    "caseName": ("case_name", "caseName"),
    "citations": ("citations", "citation"),
    "dateFiled": ("date_filed", "dateFiled"),
    "cluster_id": ("cluster_id", "id"),
}

# Search hits: o-type (opinion search) and r/rd (docket-with-docs / RECAP docs)
_SEARCH_O_SOURCES: dict[str, tuple[str, ...]] = {
    "caseName": ("caseName", "case_name"),
    "citations": ("citation", "citations"),
    "court": ("court", "court_id"),
    "dateFiled": ("dateFiled", "date_filed"),
    "cluster_id": ("cluster_id",),
    "docket_id": ("docket_id",),
}

_SEARCH_RD_SOURCES: dict[str, tuple[str, ...]] = {
    "caseName": ("caseName", "case_name"),
    "citations": ("citation", "citations"),
    "court": ("court", "court_id"),
    "dateFiled": ("dateFiled", "date_filed"),
    "cluster_id": ("cluster_id",),
    "docket_id": ("docket_id",),
    "docketNumber": ("docketNumber", "docket_number"),
    "snippet": ("snippet",),
}

# d-type (dockets search): no snippet, docket-shaped
_SEARCH_D_SOURCES: dict[str, tuple[str, ...]] = {
    "caseName": ("caseName", "case_name"),
    "citations": ("citation", "citations"),
    "court": ("court", "court_id"),
    "dateFiled": ("dateFiled", "date_filed"),
    "cluster_id": ("cluster_id",),
    "docket_id": ("docket_id",),
    "docketNumber": ("docketNumber", "docket_number"),
}

# Public allowlist per FR-2.1 (used by tests to assert key-set ⊆ allowlist)
SEARCH_HIT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "caseName",
        "citations",
        "court",
        "dateFiled",
        "cluster_id",
        "docket_id",
        "docketNumber",
        "snippet",
    }
)


def _strip_keyed(data: dict[str, Any], sources: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    """Build the stripped dict, including only present, non-None values."""
    out: dict[str, Any] = {}
    for out_key, candidates in sources.items():
        if out_key == "court":
            court_value = _normalize_court(_pick(data, candidates))
            if court_value is not None:
                out[out_key] = court_value
        elif out_key.endswith("_id"):
            id_value = _pick_id(data, candidates)
            if id_value is not None:
                out[out_key] = id_value
        else:
            value = _pick(data, candidates)
            if value is not None:
                out[out_key] = value
    return out


def extract_opinion_text(data: dict[str, Any]) -> str | None:
    """Extract opinion text with the ADR-3 fallback chain (R3).

    ``plain_text`` → ``html_with_citations`` (tags stripped) → ``html`` (tags
    stripped). Returns None when no text variant is available — callers then
    attach a ``text_unavailable`` note; stripping itself is never fatal.

    """
    plain = _pick_text(data, ("plain_text",))
    if plain is not None:
        return plain
    for html_key in ("html_with_citations", "html"):
        html_value = _pick_text(data, (html_key,))
        if html_value is not None:
            return _html_to_text(html_value)
    return None


def strip_resource(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    """Strip a detail/lookup resource to the FR-3.1 normalized schema.

    Args:
        kind: One of ``cluster``, ``opinion``, ``docket``.
        data: Raw CourtListener JSON object (snake_case detail shape or a
            merged cluster+opinion payload).

    Returns:
        dict with the FR-3.1 stable camelCase keys for ``kind``; allowlisted
        fields that are absent or null in ``data`` are omitted entirely.

    Raises:
        ValueError: If ``kind`` is not a known resource kind.

    """
    if kind == "cluster":
        return _strip_keyed(data, _CLUSTER_SOURCES)
    if kind == "docket":
        return _strip_keyed(data, _DOCKET_SOURCES)
    if kind == "opinion":
        stripped = _strip_keyed(data, _OPINION_SOURCES)
        opinion_text = extract_opinion_text(data)
        if opinion_text is not None:
            stripped["opinionText"] = opinion_text
        return stripped
    raise ValueError(f"Unknown resource kind for stripping: {kind!r}")


def _lead_snippet(hit: dict[str, Any]) -> Any:
    """Return the lead (first) sub-opinion snippet for an o-type hit (R2)."""
    opinions = hit.get("opinions")
    if isinstance(opinions, list) and opinions:
        first = opinions[0]
        if isinstance(first, dict) and first.get("snippet") is not None:
            return first["snippet"]
    return None


def strip_search_hit(hit: dict[str, Any], search_type: str) -> dict[str, Any]:
    """Strip a single v4 search hit to the FR-2.1 allowlist.

    Snippet source per R2: top-level ``snippet`` for ``r``/``rd`` types; the
    lead sub-opinion's ``opinions[0].snippet`` for ``o``-type hits; none for
    ``d``-type hits (no snippets on dockets).

    """
    if search_type == "o":
        stripped = _strip_keyed(hit, _SEARCH_O_SOURCES)
        snippet = _lead_snippet(hit)
        if snippet is None:  # some o-hits expose a top-level snippet instead
            snippet = hit.get("snippet")
        if snippet is not None:
            stripped["snippet"] = snippet
        return stripped
    if search_type in ("r", "rd"):
        return _strip_keyed(hit, _SEARCH_RD_SOURCES)
    # "d" and any unrecognized type fall back to the docket-shaped allowlist
    return _strip_keyed(hit, _SEARCH_D_SOURCES)


def strip_candidates(clusters: list[Any]) -> list[dict[str, Any]]:
    """Strip citation-lookup cluster objects to the FR-3.1 candidate schema.

    Non-dict entries are skipped rather than raising (NFR-6).

    """
    return [
        _strip_keyed(cluster, _CANDIDATE_SOURCES)
        for cluster in clusters
        if isinstance(cluster, dict)
    ]
