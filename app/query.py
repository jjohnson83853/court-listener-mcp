"""Natural-language → search-parameter adapter (FR-5, ADR-6, D5).

Pure functions: no I/O, no LLM. ``parse_natural_query`` deterministically maps
a free-text case question onto the search tools' typed parameters:

- court aliases (``"supreme court"``, ``"ninth circuit"``, …) → ``court`` id
- date phrases (``"after 2015"``, ``"since 2015-06"``, ``"before 2020"``) →
  ``filed_after`` / ``filed_before`` (YYYY-MM-DD; bare years expand to the
  first/last day)
- quoted phrases → quoted ``q`` terms (kept with their quotes)
- ``judge X`` / ``justice X`` → ``judge``; ``case X`` / ``X v. Y`` → ``case_name``

Everything unmapped stays in ``q`` verbatim (FR-5.4 — user intent is never
silently dropped). The worst case for any input is an all-terms passthrough.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date

_YEAR_MIN, _YEAR_MAX = 1750, 2200


@dataclass
class NaturalQuery:
    """Interpreted search parameters (empty string = not set)."""

    q: str = ""
    court: str = ""
    judge: str = ""
    case_name: str = ""
    filed_after: str = ""
    filed_before: str = ""

    def params(self) -> dict[str, str]:
        """The interpreted parameters as a dict (unset fields omitted)."""
        return {key: value for key, value in asdict(self).items() if value}


# Longest alias first so "supreme court of the united states" wins over
# "supreme court". Keys are matched case-insensitively as whole phrases.
_COURT_ALIASES: tuple[tuple[str, str], ...] = (
    ("supreme court of the united states", "scotus"),
    ("supreme court", "scotus"),
    ("scotus", "scotus"),
    ("ninth circuit", "ca9"),
    ("ca9", "ca9"),
    ("tenth circuit", "ca10"),
    ("ca10", "ca10"),
    ("fifth circuit", "ca5"),
    ("ca5", "ca5"),
    ("second circuit", "ca2"),
    ("ca2", "ca2"),
    ("third circuit", "ca3"),
    ("ca3", "ca3"),
    ("fourth circuit", "ca4"),
    ("sixth circuit", "ca6"),
    ("seventh circuit", "ca7"),
    ("eighth circuit", "ca8"),
    ("eleventh circuit", "ca11"),
    ("dc circuit", "cadc"),
    ("d.c. circuit", "cadc"),
    ("cadc", "cadc"),
    ("federal circuit", "cafc"),
    ("cafc", "cafc"),
)

_QUOTED_RE = re.compile(r'"([^"]*)"')
_AFTER_RE = re.compile(r"\b(?:after|since)\s+(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?\b", re.IGNORECASE)
_BEFORE_RE = re.compile(
    r"\b(?:before|through)\s+(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?\b", re.IGNORECASE
)
_JUDGE_RE = re.compile(
    r"\b[Jj](?:udge|ustice)\s+([A-Za-z][\w'.-]*(?:\s+[A-Z][\w'.-]*){0,2})"
)
_CASE_KEYWORD_RE = re.compile(
    r"\bcase\s+([^,]+?)(?=\s*,|\s+(?:after|before|since|through)\b|$)", re.IGNORECASE
)
_X_V_Y_RE = re.compile(
    r"\b([A-Za-z][\w'.]*(?:\s+[A-Za-z][\w'.]*)?\s+v\.?\s+[A-Za-z][\w'.]*(?:\s+[A-Za-z][\w'.]*)?)\b"
)
_SEPARATORS_RE = re.compile(r"[,;]")

_MatchMapper = Callable[[re.Match[str]], str | None]


def _valid_ymd(year: int, month: int, day: int) -> bool:
    """Year in range AND a calendar-real date (rejects e.g. 2015-02-30)."""
    if not (_YEAR_MIN <= year <= _YEAR_MAX):
        return False
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _filed_after_from(match: re.Match[str]) -> str | None:
    """``after/since <date|year>`` → YYYY-MM-DD (bare year → January 1st)."""
    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else 1
    day = int(match.group(3)) if match.group(3) else 1
    if not _valid_ymd(year, month, day):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _filed_before_from(match: re.Match[str]) -> str | None:
    """``before/through <date|year>`` → YYYY-MM-DD (bare year → December 31st)."""
    year = int(match.group(1))
    month_raw, day_raw = match.group(2), match.group(3)
    if not (_YEAR_MIN <= year <= _YEAR_MAX):
        return None
    if day_raw:
        month, day = int(month_raw), int(day_raw)
        if not _valid_ymd(year, month, day):
            return None
        return f"{year:04d}-{month:02d}-{day:02d}"
    if month_raw:
        month = int(month_raw)
        if not 1 <= month <= 12:
            return None
        last_day = calendar.monthrange(year, month)[1]
        return f"{year:04d}-{month:02d}-{last_day:02d}"
    return f"{year:04d}-12-31"


def _consume(
    pattern: re.Pattern[str], text: str, mapper: _MatchMapper
) -> tuple[str | None, str]:
    """First valid match → (mapped value, text with the match removed)."""
    for match in pattern.finditer(text):
        value = mapper(match)
        if value is not None:
            return value, (text[: match.start()] + " " + text[match.end() :])
    return None, text


def _extract_court(text: str) -> tuple[str | None, str]:
    """First court alias (longest phrases tried first) → court id."""
    lowered = text.lower()
    for alias, court_id in _COURT_ALIASES:
        match = re.search(rf"\b{re.escape(alias)}\b", lowered)
        if match:
            return court_id, (text[: match.start()] + " " + text[match.end() :])
    return None, text


def _extract_judge(text: str) -> tuple[str | None, str]:
    match = _JUDGE_RE.search(text)
    if match is None:
        return None, text
    return match.group(1).strip(), (text[: match.start()] + " " + text[match.end() :])


def _extract_case_name(text: str) -> tuple[str | None, str]:
    """``case X`` hint or an ``X v. Y`` pattern → case_name."""
    keyword_match = _CASE_KEYWORD_RE.search(text)
    if keyword_match is not None and keyword_match.group(1).strip():
        name = keyword_match.group(1).strip()
        return name, (text[: keyword_match.start()] + " " + text[keyword_match.end() :])
    match = _X_V_Y_RE.search(text)
    if match is not None:
        return match.group(1).strip(), (text[: match.start()] + " " + text[match.end() :])
    return None, text


def parse_natural_query(text: str) -> NaturalQuery:
    """Deterministically map a free-text case question to typed search params.

    Never raises on any input; unmapped content stays in ``q`` verbatim
    (FR-5.4).

    """
    if not isinstance(text, str):  # defensive: callers pass strings, but stay safe
        text = str(text)

    query = NaturalQuery()
    remaining = text

    # 1. Quoted phrases → quoted q terms (kept verbatim, quotes included).
    quoted_terms = [f'"{phrase.strip()}"' for phrase in _QUOTED_RE.findall(remaining)]
    remaining = _QUOTED_RE.sub(" ", remaining)

    # 2. Case-name hints ("case X", "X v. Y").
    case_name, remaining = _extract_case_name(remaining)
    query.case_name = case_name or ""

    # 3. Judge/justice hints.
    judge, remaining = _extract_judge(remaining)
    query.judge = judge or ""

    # 4. Date phrases.
    after, remaining = _consume(_AFTER_RE, remaining, _filed_after_from)
    query.filed_after = after or ""
    before, remaining = _consume(_BEFORE_RE, remaining, _filed_before_from)
    query.filed_before = before or ""

    # 5. Court aliases (whole-phrase, case-insensitive).
    court, remaining = _extract_court(remaining)
    query.court = court or ""

    # FR-5.4: everything unmapped stays in q verbatim (spacing collapsed, tokens kept).
    segments = [segment.strip() for segment in _SEPARATORS_RE.split(remaining)]
    bare_terms = [re.sub(r"\s+", " ", segment).strip() for segment in segments]
    query.q = " AND ".join(part for part in quoted_terms + bare_terms if part)

    return query
