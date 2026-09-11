# Spec Audit: court-listener-mcp upgrade (exact citation resolution + token efficiency) — REWORK-CYCLE RE-AUDIT

- **Repo:** `/home/opencode/projects/default/court-listener-mcp` @ HEAD `138d83b` + uncommitted implementation (16 modified, 10 untracked incl. `spec/`, `app/fields.py`, `app/cache.py`, `app/query.py`, 5 new test files)
- **Audit date:** 2026-09-10 · **Auditor:** spec-conformance auditor (independent re-audit; no engineer self-reports trusted; prior report used only for per-AC structure, then overwritten)
- **Posture amendment (binding):** agents must never call live CourtListener. [L] criteria AC-1.5, AC-2.5, AC-4.7, NFR-1 are audited as **mocked tests** in `tests/test_live_criteria.py` (default suite, respx, no integration markers). `tests/test_integration.py` was deleted by design — its absence is NOT a failure. Genuine live verification is a USER-MANUAL step, recorded below, not a gate.
- **Prior audit:** 22 PASS / 1 FAIL — the FAIL (missing [L] test scaffolding) is fixed by `tests/test_live_criteria.py`; this re-audit re-verified every previously-passing AC with fresh evidence.

## Summary

**28 PASS / 0 FAIL / 0 PARTIAL out of 28 AC-level checks** (27 acceptance criteria + NFR-1 mocked spot-check). Hard constraints: **11/11 PASS**. Four live-verification items **DEFERRED to user manual** per the posture amendment (not counted, not gates).

**Overall verdict: READY FOR IAC-DEVOPS.** The single prior FAIL is closed: `tests/test_live_criteria.py` adds exactly 4 mocked tests, each a faithful port of its AC onto respx transports, and all 28 AC-level checks now pass with fresh auditor-run evidence. No previously-passing AC regressed (suite grew 143 → 147 passed; the 6 pre-existing integration-marked server smoke tests still skip offline, untouched).

## Offline gates (re-run by auditor, fresh evidence)

| Command | Result |
|---|---|
| `uv run pytest` | **147 passed, 6 skipped**, exit 0 (5.74s) |
| `uv run pytest -m integration` | **6 skipped, 147 deselected**, exit 0 — zero live calls |
| `uv run mypy app` | **Success: no issues found in 11 source files** |
| `uv run ruff check app` | **All checks passed** |
| `uv run pytest tests/test_resolve_citation.py tests/test_fields.py tests/test_cache.py tests/test_query.py -v` | all PASSED (per-test names captured below) |
| `uv run pytest tests/test_mocked.py tests/test_live_criteria.py -v` | all PASSED |
| `echo ${COURT_LISTENER_API_KEY:-UNSET}` | **UNSET** — the entire 147-test default suite runs fully offline/mocked |

---

## Task-by-task results

### FR-1 — Exact citation resolution (T5, ADR-3)

- **AC-1.1** — mocked 200 lookup → cluster ID + stripped metadata + opinion text + exact expected HTTP calls
  - Verdict: **PASS**
  - Evidence: `tests/test_resolve_citation.py::TestResolveCitation200::test_ac_1_1_full_flow_and_call_count` — PASSED (auditor `-v` run; body read). Asserts `citation`/`status` echoed, cluster key set ⊆ `{caseName, citations, court, dateFiled, cluster_id, docket_id}`, `caseName == "Roe v. Wade"`, `cluster_id/docket_id/court` (court via cached-docket fallback, R1), `dateFiled`, `opinionText` content, and exact call counts: lookup=1, cluster=1, docket=1, opinion=1 — precisely the AC's "1 lookup + cluster/opinion fetches, all mocked". Companion `test_cluster_carries_court_skips_docket` PASSED (docket route count 0 when cluster carries court — ADR-3 rule).
- **AC-1.2** — mocked 300 → ≥2 stripped candidates, no cluster/opinion fetch
  - Verdict: **PASS**
  - Evidence: `TestResolveCitationDispositions::test_ac_1_2_ambiguous_300_returns_candidates_no_fetch` — PASSED (body read). Asserts `status == 300`, `len(candidates) >= 2`, each candidate keys ⊆ `{caseName, citations, dateFiled, cluster_id}` (FR-3.1 candidate schema), and `"cluster"/"opinionText"` absent from the result; only the lookup route is mocked, so any follow-up fetch would error under respx — "no fetch" is enforced, not assumed.
- **AC-1.3** — mocked 404 → structured not-found with citation echoed, no exception
  - Verdict: **PASS**
  - Evidence: `test_ac_1_3_404_not_found_no_exception` — PASSED: `is_error` False, `status == 404`, `found is False`, citation echoed. Plus `test_400_invalid_reporter` (400 branch, "Invalid citation" message) and `test_review_m4_empty_lookup_array_is_not_found` — both PASSED.
- **AC-1.4** — second identical resolution → zero HTTP (respx call count)
  - Verdict: **PASS**
  - Evidence: `TestResolveCitationCaching::test_ac_1_4_second_resolution_zero_http` — PASSED (body read): two identical `citation_resolve_citation` calls; every route count stays at 1 (second call made zero HTTP); second result identical. Also FR-1.4/NFR-2 evidence.
- **AC-1.5 [L→mocked]** — `410 U.S. 113` → Roe cluster + opinion text; `999 U.S. 9999` → not-found without error, no follow-up HTTP
  - Verdict: **PASS** (mocked, per posture amendment)
  - Evidence: `tests/test_live_criteria.py::test_ac_1_5_roe_resolves_999_not_found` — PASSED (full body read). **Faithfulness:** fixtures mirror the documented v4 shapes (design §1.2) and real Roe IDs (cluster 6605, docket 2759). Roe branch asserts cluster keys ⊆ FR-3.1 schema, "roe"+"wade" in caseName, `cluster_id` present, `court == "scotus"` via cached-docket fallback (R1), and non-empty string `opinionText`. 999 branch asserts `is_error` False, `found is False`, citation echoed, `status in (400, 404)`, message present — and **no follow-up HTTP**: `lookup.call_count == 2` (one POST per distinct citation — distinct cache slugs), while cluster/docket/opinion routes each stay at 1 (unchanged by the bogus call). No integration marker; runs in the default offline suite.

### FR-2 — Snippet-only discovery (T6, ADR-5, D2)

- **AC-2.1** — hit key set ⊆ FR-2.1 allowlist; snippet present for `o`-type hits (nested `opinions[]`)
  - Verdict: **PASS**
  - Evidence: `tests/test_fields.py::TestStripSearchHit::test_ac_2_1_o_hit_keys_allowlisted_and_snippet_present` + `test_ac_2_1_rd_hit_top_level_snippet` — PASSED; `test_d_type_hit_no_snippet` (d stripped, no snippet) and `test_o_hit_no_nested_opinions_snippet_omitted` (degrade, not fatal) — PASSED. `SEARCH_HIT_ALLOWLIST` (`app/fields.py:207–218`) = `{caseName, citations, court, dateFiled, cluster_id, docket_id, docketNumber, snippet}` (ADR-5 allowlist incl. `docketNumber`); `_STRIPPED_TYPES = frozenset({"o","d","r","rd"})` (`app/tools/search.py:23`) — all four tools stripped.
- **AC-2.2** — default limit 10; `hit` never exceeds the documented cap
  - Verdict: **PASS**
  - Evidence: `tests/test_mocked.py::test_ac_2_2_default_limit_is_10` (respx captures `hit=10`) + `test_ac_2_2_signature_default_10_cap_50` (FastMCP tool metadata: `default == 10`, `maximum == 50`, `minimum == 1` on all four search tools) — PASSED (bodies read).
- **AC-2.3** — `highlight=on` for `o`/`r`/`rd`; absent for `d`
  - Verdict: **PASS**
  - Evidence: `test_ac_2_3_highlight_sent_per_type` — PASSED (body read): asserted per type — `on` for opinions/dockets_with_documents/recap_documents, `None` for dockets.
- **AC-2.4** — `len(json.dumps(stripped)) < 0.5 * len(json.dumps(raw))` for search fixture
  - Verdict: **PASS**
  - Evidence: `test_ac_2_4_search_payload_reduction` — PASSED (body read): verbose fixture (meta/cited_blocks/absolute_url/score junk) vs stripped envelope, ratio asserted `< 0.5`. Envelope is ADR-5's `{count, results}` — cursor URLs dropped (`search.py:98–105`).
- **AC-2.5 [L→mocked]** — search `qualified immunity` + `court=scotus` → ≤10 stripped hits with snippets; chosen `cluster_id` fetched once, then cache-served
  - Verdict: **PASS** (mocked, per posture amendment)
  - Evidence: `tests/test_live_criteria.py::test_ac_2_5_snippet_search_then_fetch_once` — PASSED (full body read). **Faithfulness:** request params captured (`q == "qualified immunity"`, `court == "scotus"`, `highlight == "on"` — FR-2.3 honored); `{count, results}` envelope with exactly 10 hits (≤10 ✓); every hit keys ⊆ `SEARCH_HIT_ALLOWLIST`; snippet is a non-empty string equal to the nested `opinions[0].snippet` **verbatim** (R2); first hit's `cluster_id` → `get_cluster` returns `caseName == "Saucier v. Katz"` + `court == "scotus"`; cache file `clusters/cluster-<id>.json` exists (FR-4.1 path); second identical call returns identical data with cluster route count pinned at 1 — **fetch-once** proven. The AC's "full text once" element is decomposed on mocked transports: cluster-side fetch-once here, opinion-text fetch-once in the NFR-1 test (`opinion_route.call_count == 1` across two `get_opinion` calls). Combined coverage matches the amendment's framing ("≤10 allowlisted hits with snippets + fetch-once").

### FR-3 — Field stripping (T1, ADR-1)

- **AC-3.1** — table-driven fixtures: output keys ⊆ allowlist, required keys present
  - Verdict: **PASS**
  - Evidence: `test_ac_3_1_keys_subset_of_allowlist_and_required_present[cluster-detail]`, `[opinion-detail]`, `[docket-detail]` + `test_ac_3_1_cluster_values` — all PASSED. Source maps (`app/fields.py:140–172`) implement FR-3.1 exactly: cluster/opinion `{caseName, citations, court, dateFiled, cluster_id, docket_id}` (+`opinionText` for opinions), docket `{caseName, docketNumber, court, dateFiled, docket_id}`, candidates `{caseName, citations, dateFiled, cluster_id}` (no court — finding 1.2).
- **AC-3.2** — casing normalization: snake_case detail and camelCase hit → identical output keys
  - Verdict: **PASS**
  - Evidence: `test_ac_3_2_casing_normalization_identical_keys` — PASSED. Every source map lists both spellings per output key (e.g. `"caseName": ("case_name", "caseName")`, `fields.py:141–163`).
- **AC-3.3** — per fixture, stripped < 0.5 × raw
  - Verdict: **PASS**
  - Evidence: `test_ac_3_3_at_least_two_x_reduction[cluster-detail]/[opinion-detail]/[docket-detail]` + `test_ac_3_3_candidates_reduction` — all PASSED; flow-level corroboration via AC-2.4 (search) and NFR-1 (opinion).
- **AC-3.4** — `grep -rn "response.json()" app/tools/`: no raw pass-through on stripped paths
  - Verdict: **PASS**
  - Evidence (auditor grep, 6 sites): `get.py:68` inside `_fetch_resource` (docstring :39: "Designated raw fetch helper (AC-3.4): response.json() lives only here"); `search.py:96` inside `_search_courtlistener` with immediate strip for `_STRIPPED_TYPES` (`:98–105`; raw only for audio/people — non-goals); `citation.py:148` inside `batch_lookup_citations` — **stripped before return** via `fields.strip_candidates` (D3, code read at :148–160); `citation.py:324` inside `resolve_citation`'s internal lookup helper (result returned as stripped FR-1.3 shape); `citation.py:80` (`lookup_citation`) and `:779` (`enhanced_citation_lookup`) — pre-existing analyze tools **outside FR-3.2's stripped-path enumeration**, kept raw per NFR-5's "existing tool names and parameters otherwise unchanged". No stripped path returns raw (opt-out is the D4-sanctioned exception).

### FR-4 — Cache by stable IDs (T2–T4, ADR-2)

- **AC-4.1** — two consecutive same-ID calls → exactly 1 HTTP request total
  - Verdict: **PASS**
  - Evidence: `test_ac_4_1_cluster_second_call_zero_http` (cluster route count == 1 across two calls) + `test_ac_4_1_opinion_second_call_zero_new_http` (1 opinion + 1 cluster fetch total) — PASSED (bodies read).
- **AC-4.2** — cache files under expected namespaces/names
  - Verdict: **PASS**
  - Evidence: `test_ac_4_2_file_paths_and_names` + `test_ac_4_2_all_four_namespaces` (cache level) + `test_ac_4_2_cache_files_under_expected_paths` (flow level: `clusters/cluster-123.json` exists) — PASSED.
- **AC-4.3** — TTL expiry → fresh HTTP + rewrite
  - Verdict: **PASS**
  - Evidence: `test_ac_4_3_static_ttl_expiry_is_a_miss` + `test_ac_4_3_rewrite_after_expiry` (fake-time, no sleep; body read: put → +61s → miss → rewrite → hit) + `test_ac_4_3_expired_entry_triggers_fresh_http` (mocked flow) — PASSED. `test_dockets_use_short_ttl` (D1 split) — PASSED.
- **AC-4.4** — size cap evicts oldest first
  - Verdict: **PASS**
  - Evidence: `test_ac_4_4_cap_evicts_oldest_first` — PASSED (body read): deterministic mtimes via `os.utime`, cap sized to the two newest, oldest evicted first.
- **AC-4.5** — `.cache/` gitignored
  - Verdict: **PASS**
  - Evidence: `grep -n "cache" .gitignore` → line 46 `.cache` (non-empty ✓). Bonus R5: `.dockerignore:77` `.cache/`.
- **AC-4.6** — no cache file ever contains the API key or auth headers
  - Verdict: **PASS**
  - Evidence: `test_ac_4_6_no_key_material_in_entries` + `test_no_auth_headers_storable` (cache level, serialized-content assertions) + `test_ac_4_6_no_key_material_in_cache_after_flow` (flow level) — all PASSED.
- **AC-4.7 [L→mocked]** — after "restart", previously-fetched cluster served from cache with zero HTTP
  - Verdict: **PASS** (mocked, per posture amendment)
  - Evidence: `tests/test_live_criteria.py::test_ac_4_7_cache_survives_context_renewal` — PASSED (full body read). **Faithfulness:** a restart is proxied by `set_cache(FileCache(root=<same root>))` — a brand-new cache instance over the persisted files, which is exactly the post-restart state for a pure file cache (the file layer is the only persistence). Zero HTTP after renewal is asserted **two independent ways**: respx route counts unchanged AND a monkeypatched counting spy on the designated raw fetch helper `app.tools.get._fetch_resource` records **0** invocations; data identical to the pre-restart fetch; a fresh `FileCache` also serves the entry by direct read. This is stronger than the AC's original log-line evidence.

### FR-5 — NL → query adapter (T7, ADR-6, D5)

- **AC-5.1** — ≥8 table-driven cases; exact parameter dicts
  - Verdict: **PASS**
  - Evidence: `test_ac_5_1_exact_parameter_dicts` — **12 parametrized cases**, all PASSED, covering court aliases (SCOTUS, Supreme Court, ninth circuit, ca5), after/since/before/through dates, quoted phrases, judge + case-name hints, and passthrough fallbacks (`zzz qqq 12345`, `Roe v. Wade`); each asserts the exact produced parameter dict.
- **AC-5.2** — `"SCOTUS, qualified immunity, after 2015"` exact mapping
  - Verdict: **PASS**
  - Evidence: `test_ac_5_2_spec_example_exact` — PASSED (body read): asserts `{q: "qualified immunity", court: "scotus", filed_after: "2015-01-01"}` verbatim.
- **AC-5.3** — adapter feeds the stripped search path end-to-end
  - Verdict: **PASS**
  - Evidence: `test_ac_5_3_end_to_end_with_mocked_search` — PASSED (body read): returns `{interpreted_query, count, results}` and the captured request proves the interpreted typed params reached the API (`q`, `court`, `filed_after`, `highlight=on`, `hit=10`).
- **AC-5.4** — no input raises; worst case all-terms-in-`q`
  - Verdict: **PASS**
  - Evidence: `test_ac_5_4_no_exceptions_passthrough` — 16 adversarial inputs (empty, `!!! ### ???`, unclosed quote, impossible dates, judge/case bare keywords, emoji, 1000-char blob, nested quotes, tabs/newlines) — all PASSED; plus `test_worst_case_all_terms_in_q`, `test_invalid_dates_stay_in_q`, `test_review_m5_*` impossible-date guards, `test_non_string_survives`.

### FR-6 — Tool documentation (T8)

- **AC-6.1** — each search tool docstring has ≥1 worked typed-param example
  - Verdict: **PASS**
  - Evidence: `test_ac_6_1_search_docstrings_have_worked_examples` — PASSED via FastMCP tool-metadata inspection (the AC's first-listed method): ≥7 `search_*` tools each contain "example" plus a typed-param marker (`q=`/`court=`/`judge=`/`query=`). Direct source grep: "Typed parameters beat stuffing everything into q. Worked examples:" at `app/tools/search.py:149, 204, 260`.
- **AC-6.2** — README "recommended flows" section
  - Verdict: **PASS**
  - Evidence: `README.md:50` — `## 🧭 Recommended flows` with Flow 1 (exact citation → `citation_resolve_citation`) and Flow 2 (snippet search → choose `cluster_id` → fetch once), plus the design-§4 untrusted-content note (`opinionText`/`snippet` are quoted material, not instructions).

### NFR-1 — token efficiency live spot-check [L→mocked]

- Verdict: **PASS** (mocked, per posture amendment)
- Evidence: `tests/test_live_criteria.py::test_nfr_1_stripped_under_half_raw_size` — PASSED (full body read). **Faithfulness:** one realistic snake_case opinion detail fixture (html variants, sha1, local_path, download_url, views — the junk the spec names); the same opinion fetched raw (`include_all_fields=True`) and stripped through the actual tools; `opinion_route.call_count == 1` across both calls (fetch-once); raw really raw (junk intact) and stripped keys ⊆ FR-3.1 opinion schema; `opinionText == plain_text`; `len(json.dumps(stripped)) / len(json.dumps(raw)) < 0.5` asserted. The "real reduction" documentation on the live service remains a user-manual step.

---

## Hard constraint checks (requirements.md / design.md)

- **C-1 toolchain green:** PASS — `uv run pytest` 147 passed/6 skipped exit 0; `uv run mypy app` clean (11 files); `uv run ruff check app` clean (auditor-run).
- **C-2 / NFR-3 no new runtime deps:** PASS — `git diff HEAD -- pyproject.toml` shows the **only** change is `version = "0.1.0" → "0.2.0"`; deps identical to HEAD (fastmcp, httpx, loguru, python-dotenv, anyio, pydantic, pydantic-settings, psutil, citeurl[full], markdown — all pre-existing). Cache is stdlib JSON files.
- **NFR-4 secrets & permissions:** PASS — key handling unchanged (env-first, `.env` fallback, `Token` header; config.py change is additive cache fields); cache stores response data only (AC-4.6 tests green); `app/cache.py` chmods cache root `0o700` (`:109`) and entry temp files `0o600` (`:156`) — user-only, covered by `test_directory_permissions_user_only` PASSED. Key is UNSET in this environment and never required by the default suite.
- **NFR-5 version 0.2.0 + compatibility posture:** PASS — `pyproject.toml:3` and `app/__init__.py:14` (`__version__ = "0.2.0"` fallback) both 0.2.0; tools modified in place, names/params otherwise unchanged; opt-out param added (D4).
- **NFR-6 stripping/caching never fatal:** PASS — `test_nfr_6_missing_fields_omitted_never_null_padded`, `test_review_high_2_unwritable_cache_still_returns_data`, corrupt/malformed-entry-as-miss tests all PASSED; code paths catch OSError/Exception as warnings.
- **D1 cache TTL/cap defaults:** PASS — `test_config_defaults_match_d1` + `test_cache_config_env_overrides` + `test_filecache_reads_config_when_unspecified` PASSED (30d static / 24h dockets / 100 MB cap, env-overridable per T3).
- **D3 batch stripping disposition:** PASS — `test_d3_batch_strips_clusters_keeps_statuses` PASSED; `citation.py:148–160` strips candidate clusters via `fields.strip_candidates` while preserving counts and per-citation statuses (code read).
- **D4 in-place change + opt-out:** PASS — `include_all_fields: bool = False` on `opinion`/`cluster`/`docket` (verified via `test_ac_3_3_include_all_fields_is_real_parameter_default` on real signatures + `test_include_all_fields_opt_out_returns_raw` returning the raw payload with junk fields intact); `audio`/`person` untouched (non-goals).
- **D5 adapter + docs disposition:** PASS — pure `app/query.py` (`parse_natural_query`, no I/O) + `search_natural_language` tool + FR-6 docstring examples and README flows all present and tested.
- **FR-4.5/FR-4.6 cache behaviors:** PASS — cache checked before HTTP (`cached_fetch`: get → miss → fetch → put, `get.py:78–103`); search results never cached (`_search_courtlistener` returns directly, no cache namespace); atomic writes via temp + `os.replace` (`cache.py:141–156`, `test_no_tmp_files_left_after_put` PASSED).
- **Posture amendment (no agent live calls):** PASS — `grep -rn "pytest.mark.integration" tests/` → exactly 6 markers, all in `tests/test_server.py:114,151,176,214,262,299` (pre-existing server smoke tests, untouched by design); `conftest.py:33` skips integration-marked nodes when the key is absent; `tests/test_live_criteria.py` carries **no** marker (line 6 is a docstring mention) and routes every HTTP through respx — an unmocked request would raise and fail the test; the full default suite is green with `COURT_LISTENER_API_KEY=UNSET`, so **zero live calls occurred during this audit**. `tests/test_integration.py` absent from the tree = deleted by design (was live-only) — not a failure.
- **Non-goals respected:** PASS — audio/people search and get stay raw (`test_oa_and_people_search_stay_raw` PASSED; `get.py` audio/person call `_fetch_resource` directly); 429 surfaced not retried (`test_429_surfaced_not_retried` PASSED); search not cached; no pagination machinery.

---

## Deferred — user-manual live verification (outside the agent pipeline; NOT gates)

Per the binding posture amendment, these are recorded for the user's manual, not scored:

1. **AC-1.5 real-service check:** `resolve_citation("410 U.S. 113")` → Roe cluster + opinion text; `"999 U.S. 9999"` → structured not-found (mocked equivalent verified this cycle).
2. **AC-2.5 real-service check:** live `qualified immunity` + `court=scotus` snippet search (≤10 hits) → fetch chosen cluster once, cache-served repeat.
3. **AC-4.7 real restart + NFR-1 real-reduction documentation:** restart the deployed server, confirm zero HTTP on a cached cluster; document the real stripped-vs-raw reduction for one opinion fetch.
4. **R1/A-1 real-corpus confirmation** (design risk, tasks.md T10): confirm cluster-detail field casing and `court` availability on a real cluster; if reality differs, adjust the data-driven source maps in `fields.py` (designed for exactly this correction). Optionally run `uv run pytest -m integration` with `COURT_LISTENER_API_KEY` set to exercise the 6 pre-existing server smoke tests.
5. **T10/T12/T13** (integration gate with key, user-executed image build/push to `registry.localdomain:5000`, deploy + smoke) — user/iac-devops scope per C-4/C-5.

## Other observations (non-blocking, for code-mentor/code-review)

1. **README Flow 2 comment overstates:** the inline comment `get_cluster(cluster_id=<your pick>)   # full text fetched once, then cached` says "full text", but `get_cluster` returns stripped cluster *metadata* (no `opinionText`). Full text is reached via `citation_resolve_citation` (using the hit's citation) or `get_opinion` (opinion ID via the raw opt-out). Cosmetic doc nit; the flow itself works as designed.
2. **AC-2.5 "full text" decomposition:** the cluster-side fetch-once is proven in `test_ac_2_5`; opinion-text fetch-once is proven in the NFR-1 mocked test. Combined mocked coverage is complete; the genuine live end-to-end wording remains user-manual (item 2 above).
3. **`lookup_citation` / `enhanced_citation_lookup` keep raw payloads** — spec-conformant (outside FR-3.2's stripped-path enumeration; D3 covers batch only) but worth a conscious confirm at live-verification time if token budget matters.
4. **FastMCP deprecation warnings** (`import_server` → `mount()`, 3 warnings) — pre-existing, out of audit scope.
5. **Tree is uncommitted** (16 modified + 10 untracked) — engineer work complete per delivery flow; iac-devops opens the PR (C-4). Reminder only.
6. **docker-compose.yml still at pre-upgrade state** (`build: .` + `image: court-listener-mcp:latest`, no cache bind mount, no cache env vars) — this is T11 (iac-devops) scope, not an engineer defect; see handoff.