"""Tests for the natural-language query adapter (FR-5, ADR-6, D5).

AC coverage:
- AC-5.1: ≥8 table-driven cases (court aliases, after/before/since dates,
  quoted phrases, judge hints, passthrough fallbacks) asserting the exact
  produced parameter dict.
- AC-5.2: exact 'SCOTUS, qualified immunity, after 2015' mapping.
- AC-5.3: adapter output feeds the search path end-to-end (mocked search).
- AC-5.4: adversarial inputs never raise; worst case is all-terms passthrough.
"""

from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from app.query import parse_natural_query

# (input, expected exact parameter dict) — AC-5.1 table
PARSE_CASES: list[tuple[str, dict[str, str]]] = [
    # Court aliases
    ("SCOTUS, qualified immunity, after 2015",
     {"q": "qualified immunity", "court": "scotus", "filed_after": "2015-01-01"}),
    ("Supreme Court, qualified immunity",
     {"q": "qualified immunity", "court": "scotus"}),
    ("ninth circuit section 1983",
     {"q": "section 1983", "court": "ca9"}),
    ("ca5, maritime, before 1990",
     {"q": "maritime", "court": "ca5", "filed_before": "1990-12-31"}),
    # Date phrases
    ("qualified immunity since 2015-06",
     {"q": "qualified immunity", "filed_after": "2015-06-01"}),
    ("seaman status before 2020",
     {"q": "seaman status", "filed_before": "2020-12-31"}),
    ("certiorari through 2020-03",
     {"q": "certiorari", "filed_before": "2020-03-31"}),
    ("frisk after 1968-02-12",
     {"q": "frisk", "filed_after": "1968-02-12"}),
    # Quoted phrases
    ('ninth circuit "official capacity" after 2015',
     {"q": '"official capacity"', "court": "ca9", "filed_after": "2015-01-01"}),
    # Judge / case-name hints
    ("judge Sonia Sotomayor case Miranda since 2015-06",
     {"judge": "Sonia Sotomayor", "case_name": "Miranda", "filed_after": "2015-06-01"}),
    ("Roe v. Wade", {"case_name": "Roe v. Wade"}),
    # Passthrough fallback (nothing mappable)
    ("zzz qqq 12345", {"q": "zzz qqq 12345"}),
]


class TestParseNaturalQuery:
    """AC-5.1 / AC-5.2 — deterministic parameter mapping."""

    @pytest.mark.parametrize(("text", "expected"), PARSE_CASES)
    def test_ac_5_1_exact_parameter_dicts(
        self, text: str, expected: dict[str, str]
    ) -> None:
        """AC-5.1: each input maps to the exact expected parameter dict."""
        assert parse_natural_query(text).params() == expected

    def test_ac_5_2_spec_example_exact(self) -> None:
        """AC-5.2: 'SCOTUS, qualified immunity, after 2015' exact mapping."""
        assert parse_natural_query("SCOTUS, qualified immunity, after 2015").params() == {
            "q": "qualified immunity",
            "court": "scotus",
            "filed_after": "2015-01-01",
        }

    def test_mixed_mapped_and_unmapped_terms(self) -> None:
        """FR-5.4: mapped phrases consumed; unmapped tokens (even filler) stay in q."""
        parsed = parse_natural_query("opinions in scotus about seaman after 2000")
        assert parsed.params() == {
            "q": "opinions in about seaman",
            "court": "scotus",
            "filed_after": "2000-01-01",
        }

    def test_quoted_phrase_kept_with_quotes(self) -> None:
        """FR-5.2: quoted phrases become quoted q terms (AND-joined with bare terms)."""
        parsed = parse_natural_query('"official capacity" SCOTUS')
        assert parsed.params() == {
            "q": '"official capacity"',
            "court": "scotus",
        }

    def test_invalid_dates_stay_in_q(self) -> None:
        """FR-5.4: month 13 is not a date phrase — it stays in q."""
        parsed = parse_natural_query("ruling after 2015-13 before 3000")
        assert "2015-13" in parsed.q
        assert "3000" in parsed.q
        assert parsed.filed_after == ""
        assert parsed.filed_before == ""

    def test_review_m5_impossible_date_stays_in_q(self) -> None:
        """M5: calendar-impossible 2015-02-30 never becomes an API date filter."""
        parsed = parse_natural_query("ruling after 2015-02-30")
        assert parsed.filed_after == ""
        assert "2015-02-30" in parsed.q

    def test_review_m5_impossible_before_date_stays_in_q(self) -> None:
        """M5: 2015-02-30 as a before-date also stays in q."""
        parsed = parse_natural_query("ruling before 2015-02-30")
        assert parsed.filed_before == ""
        assert "2015-02-30" in parsed.q


class TestNaturalLanguageSearchTool:
    """AC-5.3 — adapter feeds the stripped search path end-to-end."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_ac_5_3_end_to_end_with_mocked_search(
        self, client: Client[Any]
    ) -> None:
        """AC-5.3: search_natural_language → interpreted params + stripped hits."""
        raw_response = {
            "count": 1,
            "next": None,
            "previous": None,
            "results": [
                {
                    "id": 1000,
                    "caseName": "Smith v. Jones",
                    "dateFiled": "2016-01-04",
                    "court": "scotus",
                    "citation": ["123 U.S. 456"],
                    "cluster_id": 123,
                    "docket_id": 55,
                    "snippet": "the <mark>qualified immunity</mark> question",
                    "score": 0.9,
                    "citeCount": 7,
                }
            ],
        }
        route = respx.get("https://www.courtlistener.com/api/rest/v4/search/").mock(
            return_value=httpx.Response(200, json=raw_response)
        )

        async with client:
            result = await client.call_tool(
                "search_natural_language",
                {"query": "SCOTUS, qualified immunity, after 2015"},
            )

        assert not result.is_error
        data = result.data
        assert data["interpreted_query"] == {
            "q": "qualified immunity",
            "court": "scotus",
            "filed_after": "2015-01-01",
        }
        assert data["count"] == 1
        assert data["results"][0]["caseName"] == "Smith v. Jones"
        assert data["results"][0]["snippet"] == "the <mark>qualified immunity</mark> question"

        # The request actually used the interpreted typed parameters.
        params = route.calls.last.request.url.params
        assert params["q"] == "qualified immunity"
        assert params["court"] == "scotus"
        assert params["filed_after"] == "2015-01-01"
        assert params["highlight"] == "on"
        assert params["hit"] == "10"


class TestAdversarialInputs:
    """AC-5.4 — no input causes an exception; worst case is full passthrough."""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            " ",
            "!!! ### ???",
            '"unclosed quote',
            "after before since since",
            "judge",
            "case",
            "v.",
            "after 9999 before 0000 since 0000-99 through 9999-99-99",
            "SCOTUS SCOTUS SCOTUS, ,, ,, after 2015 after 2010",
            "emoji 🏛️-laws after 2015",
            "x" * 5000,
            "nested \"quotes \\\" inside\" after 2015",
            "\n\t tabs and newlines ",
        ],
    )
    def test_ac_5_4_no_exceptions_passthrough(self, text: str) -> None:
        """AC-5.4: worst case is an all-terms-in-q passthrough; never raises."""
        parsed = parse_natural_query(text)
        params = parsed.params()
        assert isinstance(params, dict)
        if not params.get("court"):
            assert not params.get("filed_after") or params["filed_after"].count("-") == 2

    def test_worst_case_all_terms_in_q(self) -> None:
        """FR-5.4: gibberish input ends up entirely in q (never dropped)."""
        garbage = "flibbertigibbet 12345 !!!"
        assert parse_natural_query(garbage).params() == {"q": garbage}

    def test_non_string_survives(self) -> None:
        """Defensive: non-string input is coerced, not fatal."""
        parsed = parse_natural_query(None)  # type: ignore[arg-type]
        assert isinstance(parsed.q, str)
