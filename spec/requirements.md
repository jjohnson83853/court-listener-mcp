# requirements.md — court-listener-mcp upgrade: exact citation resolution + token efficiency

- **Repo:** `/home/opencode/projects/default/court-listener-mcp` @ HEAD `138d83b`
- **Spec location:** `spec/` (this directory)
- **Status:** DRAFT — awaiting user confirmation (SpecDD Checkpoint 1)
- **Date:** 2026-09-10

---

## 1. Goal / problem statement

`court-listener-mcp` is a FastMCP server (Python 3.12, `app/`) exposing CourtListener v4 API tools to LLM clients. Today it works, but it is **fuzzy and token-hungry**:

1. **Fuzzy discovery.** An LLM client holding an exact citation (e.g. `410 U.S. 113`) has no single tool that turns it into the matched case and its opinion text. It must string together search calls with guessed keywords and retry loops. The existing `lookup_citation` / `enhanced_citation_lookup` (`app/tools/citation.py`) return match counts and raw lookup payloads, **not** a cluster-ID → opinion-text resolution flow.
2. **Token waste.** `_fetch_resource` (`app/tools/get.py:22–63`) and `_search_courtlistener` (`app/tools/search.py:23–88`) return CourtListener JSON **verbatim** — dozens of fields (`meta`, `panel_ids`, `local_path`, `sha1`, `attorney`, `posture`, …) the LLM never needs, on every call.
3. **No cache.** The same cluster/opinion/docket is re-fetched over HTTP on every tool call (zero `.cache/` usage repo-wide).

**Goal of this upgrade:** convert citation-based case access from fuzzy search into exact resolution (canonical citation → matched cluster ID → exact opinion text), and cut token consumption via field stripping, snippet-only search results, and a file cache keyed by stable CourtListener IDs.

## 2. Users

- **Primary:** LLM clients (Claude et al.) consuming this MCP server over stdio/HTTP — they benefit from smaller tool responses and fewer retry loops.
- **Operator:** the user, running the server via `docker-compose.yml` (HTTP transport, port 8000) with `COURT_LISTENER_API_KEY` in `.env`.

## 3. Functional requirements

Each acceptance criterion is tagged:
- **[T]** — testable offline with existing pytest + `respx` HTTP-mocking setup (no API key needed).
- **[L]** — requires live CourtListener access, i.e. `COURT_LISTENER_API_KEY` must be set (see §6, Open Dependency). These run as `@pytest.mark.integration` tests, skipped when the key is absent.

### FR-1 — Exact citation resolution (upgrade 1)

Given a citation string, the server must resolve it through CourtListener's citation-lookup API to the matched opinion cluster and the exact opinion text, replacing fuzzy "search for the case" call chains.

**Behavior:**
- FR-1.1 A new tool accepts a single citation string (any format citeurl/citation-lookup recognizes: U.S. Reporter, F.3d, state reporters, etc.).
- FR-1.2 It calls `POST /api/rest/v4/citation-lookup/` (existing code path in `app/tools/citation.py:70–76`) and interprets the documented per-citation `status`:
  - `200` → exactly-resolved (or primary match): proceed with `clusters[0]`.
  - `300` (ambiguous, multiple clusters) → return the candidate list (stripped metadata per FR-3) so the client/user can disambiguate; do **not** guess.
  - `404` (valid format, not in corpus) / `400` (unknown reporter) → return a clear "not found / invalid" result; no search fallback, no retry loop.
  - `429` (over cap) → surface the status; no silent retry (see Non-goals).
- FR-1.3 On resolution, it extracts the cluster ID from the matched cluster object and fetches cluster metadata + opinion text, returning only: `caseName`, `citations`, `court`, `dateFiled`, opinion text (FR-3 schema).
- FR-1.4 The citation → cluster-ID mapping and the fetched cluster are cached (FR-4), so repeat resolutions of the same citation make zero HTTP calls after the first.

**Acceptance criteria:**
- AC-1.1 **[T]** Unit test with a mocked (respx) citation-lookup response (`status: 200`, cluster with `id`) asserts the tool returns the cluster ID, stripped case metadata, and opinion text, and makes exactly the expected HTTP calls (1 lookup + cluster/opinion fetches, all mocked).
- AC-1.2 **[T]** Mocked `status: 300` response returns ≥2 stripped candidates and performs **no** cluster/opinion fetch.
- AC-1.3 **[T]** Mocked `status: 404` returns a `not_found`-style result with the original citation echoed; no exception raised to the client.
- AC-1.4 **[T]** With FR-4's cache enabled, a second identical resolution issues zero HTTP requests (respx call-count assertion).
- AC-1.5 **[L]** Live: `410 U.S. 113` resolves to *Roe v. Wade*'s cluster with opinion text; `999 U.S. 9999` returns not-found without error.

### FR-2 — Snippet-only discovery (upgrade 2)

Search tools must return per-hit metadata + snippet only (no full search payload), with results capped at 5–10 by default; full opinion text is fetched only for the cluster the client explicitly chooses, once, then cached.

**Behavior:**
- FR-2.1 Search results (all four tools in `app/tools/search.py`: `opinions`, `dockets`, `dockets_with_documents`, `recap_documents`) return per hit only: `caseName`, `citations` (where present), `court`, `dateFiled`, `cluster_id` / `docket_id`, and the `snippet` field (top-level for `r`/`rd`-type hits; nested in `opinions[].snippet` for `o`-type hits — see design risk R2).
- FR-2.2 Default `limit` drops from 20 to **10** (within the user's 5–10 band; callers may still lower it).
- FR-2.3 The `highlight=on` request parameter is sent for search types that support snippets (`o`, `r`, `rd`) so snippets contain highlighted match terms (verified: FLP wiki "Legal Search API").
- FR-2.4 No search tool call fetches opinion text. Full text is only obtained by the client passing the chosen `cluster_id` to the FR-1/FR-3 get tools — which cache it (FR-4).

**Acceptance criteria:**
- AC-2.1 **[T]** Unit test with a realistic mocked search response (fixture modeled on the documented v4 hit shape) asserts each returned hit's key set ⊆ the FR-2.1 allowlist, and that a snippet value is present for `o`-type hits (extracted from the nested `opinions[]`).
- AC-2.2 **[T]** `opinions` tool default limit is 10 (assert via tool signature introspection or a mocked request capturing `hit=10`); the `hit` param sent to the API never exceeds the tool's documented cap.
- AC-2.3 **[T]** Mocked request capture asserts `highlight=on` is present for `opinions`/`dockets_with_documents`/`recap_documents` calls and absent for `dockets` (`d`-type, no snippet).
- AC-2.4 **[T]** For a fixture raw-vs-stripped search response, `len(json.dumps(stripped)) < 0.5 * len(json.dumps(raw))`.
- AC-2.5 **[L]** Live: a search for `qualified immunity` with `court=scotus` returns ≤10 stripped hits with snippets; passing one hit's `cluster_id` to the cluster/opinion tool returns full text once, then cache-served.

### FR-3 — Field stripping before context (upgrade 3)

All case-law fetch tools must return only the agreed field set, dropping everything else from CourtListener JSON.

**Behavior:**
- FR-3.1 Normalized output schema (stable keys regardless of API casing — search hits use camelCase, detail endpoints use snake_case):
  - Opinions/clusters: `caseName`, `citations`, `court`, `dateFiled`, `opinionText` (opinions only), `cluster_id`, `docket_id`.
  - Dockets: `caseName`, `docketNumber`, `court`, `dateFiled`, `docket_id`.
  - Citation-lookup candidates (FR-1 `300` case): `caseName`, `citations`, `dateFiled`, `cluster_id`.
- FR-3.2 Stripping applies to: the new resolution tool, `opinion`, `cluster`, `docket` (`app/tools/get.py`), all four search tools, and the candidate lists. Whether `batch_lookup_citations` / `audio` / `person` keep raw pass-through is **Open Decision D3/D4** (§7).
- FR-3.3 An explicit opt-out (e.g. `include_all_fields: bool = False`) remains available on get tools for debugging (Open Decision D4).
- FR-3.4 Unknown/missing allowlisted fields are omitted, never null-padded; unknown fields in the API response are silently dropped.

**Acceptance criteria:**
- AC-3.1 **[T]** Table-driven unit tests over fixture payloads (cluster detail, opinion detail, docket detail, search hit, citation-lookup cluster) assert output keys ⊆ allowlist and required keys present.
- AC-3.2 **[T]** Casing normalization test: a snake_case cluster detail (`case_name`, `date_filed`) and a camelCase search hit (`caseName`, `dateFiled`) produce identical output keys.
- AC-3.3 **[T]** For each fixture, `len(json.dumps(stripped)) < 0.5 * len(json.dumps(raw))`.
- AC-3.4 **[T]** `grep -rn "response.json()" app/tools/` shows no raw pass-through on the stripped paths (only the designated fetch helpers return raw data internally).

### FR-4 — Cache by stable CourtListener IDs (upgrade 4)

A file-based cache under `.cache/` with two key namespaces alongside the canonical-citation namespace:

**Behavior:**
- FR-4.1 Namespaces:
  - `citations/` — canonical citation string → citation-lookup result (cluster ID + stripped metadata).
  - `clusters/` — `cluster-{id}.json` — stripped cluster metadata.
  - `dockets/` — `docket-{id}.json` — stripped docket metadata.
  - `opinions/` — `opinion-{id}.json` — opinion text records.
- FR-4.2 Same `.cache/` mechanism/convention: plain JSON files, disposable (safe to `rm -rf`), no external services.
- FR-4.3 TTL policy is configurable; opinions/clusters (effectively immutable) get a long default TTL, dockets (mutable) a short one — exact defaults are **Open Decision D1** (§7).
- FR-4.4 A size cap with oldest-first eviction bounds disk use (default in **Open Decision D1**).
- FR-4.5 Cache is checked before any HTTP call on the cached paths (resolution, cluster/docket/opinion get); search results are **not** cached (bounded, query-dependent).
- FR-4.6 Cache writes are atomic (temp file + rename) so a concurrent read never sees partial JSON.

**Acceptance criteria:**
- AC-4.1 **[T]** Two consecutive `cluster`/`opinion` calls for the same ID issue exactly 1 HTTP request total (respx call count).
- AC-4.2 **[T]** Cache files appear under the expected namespace paths with the expected names.
- AC-4.3 **[T]** TTL expiry test (freezer/short TTL): expired entry triggers a fresh HTTP call and a rewrite.
- AC-4.4 **[T]** Size-cap test: exceeding the cap evicts the oldest entries first.
- AC-4.5 **[T]** `.cache/` is gitignored (`grep -n "cache" .gitignore` non-empty after the change).
- AC-4.6 **[T]** No cache file ever contains the API key or auth headers (assert on serialized cache content in tests).
- AC-4.7 **[L]** Live: restart the server; a previously-fetched cluster is served from cache with no HTTP request visible in logs.

### FR-5 — Natural-language → query adapter (upgrade 5)

A deterministic adapter that translates a natural-language case question into the search tools' existing typed parameters, avoiding wasted retry loops from malformed queries.

**Behavior:**
- FR-5.1 A new search tool accepts one free-text string, e.g. `"SCOTUS, qualified immunity, after 2015"`.
- FR-5.2 The adapter deterministically maps (no LLM, no network):
  - Court names/aliases → `court` param (`scotus`, `ca9`, …; small alias table incl. "Supreme Court" → `scotus`).
  - Date phrases ("after 2015", "since 2015-06", "before 2020") → `filed_after` / `filed_before` (YYYY-MM-DD).
  - Quoted phrases → quoted `q` terms; remaining terms → `q` keywords joined with `AND`.
  - Judge names preceded by "judge"/"Justice" → `judge` param; "case name" hints → `case_name`.
- FR-5.3 It then invokes the existing `opinions` search flow (FR-2-stripped results), returning the interpreted parameters alongside results so the client can see and correct the interpretation.
- FR-5.4 Unrecognized tokens pass through as plain `q` terms — the adapter must never silently drop user intent; anything it cannot confidently map stays in `q`.

**Acceptance criteria:**
- AC-5.1 **[T]** Table-driven unit tests: ≥8 cases covering court aliases, after/before/since dates, quoted phrases, judge hints, and passthrough fallbacks; each asserts the exact produced parameter dict.
- AC-5.2 **[T]** `"SCOTUS, qualified immunity, after 2015"` → `{q: 'qualified immunity', court: 'scotus', filed_after: '2015-01-01'}` (exact assertion).
- AC-5.3 **[T]** Adapter output feeds the search path end-to-end with a mocked search response (integration of adapter + stripped search).
- AC-5.4 **[T]** No input causes an exception; worst case is an all-terms-in-`q` passthrough (fuzz-ish test with adversarial strings).

### FR-6 — Tool documentation for query syntax

Tool docstrings/descriptions must teach the fielded parameters (court IDs, date filters, judge/case-name params) with concrete examples, so LLM clients use typed params directly instead of stuffing NL into `q`.

**Acceptance criteria:**
- AC-6.1 **[T]** Each search tool's docstring contains ≥1 worked example showing typed params (assert via reading tool metadata through the FastMCP client in tests, or grep on source).
- AC-6.2 README gains a short "recommended flows" section: exact citation → resolve; topic → snippet search → choose cluster → fetch once.

## 4. Non-functional requirements

- **NFR-1 Token efficiency (measurable):** stripped outputs are the default on all case-law paths; fixture-based ratio tests (AC-2.4, AC-3.3) prove ≥2× reduction per response. Live spot-check **[L]** documents the real reduction for one opinion fetch.
- **NFR-2 Latency:** cache hits avoid both HTTP round-trips and payload parsing; cached resolution path issues zero HTTP calls (AC-4.1/AC-1.4).
- **NFR-3 Operational simplicity:** no new services, containers, or runtime dependencies (stdlib + existing deps only — `pyproject.toml` deps unchanged for runtime). Cache is disposable JSON on disk.
- **NFR-4 Security:** API key handling unchanged (`app/config.py:50–84`: env-first, `.env` fallback, `Token` header); the key is never written to cache, logs, or tool output; cache directory permissions default to user-only.
- **NFR-5 Compatibility posture:** in-place change of existing tools' output shape (stripped) is acceptable — this is v0.1.0 personal tooling — with a version bump to 0.2.0 and an opt-out param (Open Decision D4). Existing tool names and parameters otherwise unchanged.
- **NFR-6 Reliability:** stripping/caching must never turn a successful API response into a tool error — missing allowlisted fields are omitted, not fatal (AC-3.1).

## 5. Non-goals (explicitly out of scope)

- **Rate-limit/retry framework** (429 backoff, circuit breakers). Cache reduces call volume; per-citation `429` statuses are surfaced, not retried. Revisit separately if throttling bites in practice.
- **Caching search results.**
- **Semantic/vector search**, full NL query semantics, or fuzzy matching beyond FR-5's deterministic grammar.
- **Stripping/normalizing `audio`, `person`, or RECAP-specific semantics beyond the shared hit-stripping.**
- **Pagination/cursor support** (v4 search is cursor-paginated; out of scope).
- **AuthN/AuthZ on the MCP server itself** (unchanged: trusted LAN HTTP on :8000).
- **DB-backed or shared cache** (single-container file cache only).

## 6. Constraints

- **C-1 Language/toolchain:** Python ≥3.12, `uv` (`uv.lock` present), FastMCP ≥2.8, httpx; tests via pytest + pytest-asyncio + respx (all already in `pyproject.toml` dev group); `mypy`/`ruff` clean.
- **C-2 No new runtime dependencies.**
- **C-3 OPEN DEPENDENCY (blocks [L] criteria only):** `COURT_LISTENER_API_KEY` is **unset in this environment**. `app/config.py:get_api_key()` (config.py:50–65) reads env first, then `.env` — so live verification requires the user to provide the key in the deployment `.env` or environment. All offline **[T]** criteria are unaffected. **This is a Class-B blocker for live checks only; engineering proceeds on mocked tests.**
- **C-4 Delivery model:** PR-based. `engineer` finishes code + offline tests; `iac-devops` opens a PR with deployment artifacts; the user reviews/merges and **builds/pushes the image themselves** to `registry.localdomain:5000` (port 5000). No agent-executed remote builds; no GitHub Actions/ghcr/Docker Hub.
- **C-5 Update pickup:** the deployed server is a persistent (`restart: unless-stopped`) container — **Watchtower** (already in this environment) handles redeploy once the new image lands in the registry. One-shot pull hooks are not applicable.
- **C-6 API behavior grounding:** CourtListener v4 behaviors cited in this spec are verified against official docs (FLP wiki: Legal Search API v4, Citation Lookup API v4) — not invented. Response-shape details that code alone can't confirm (cluster-detail field casing, court field availability on clusters, see design risks) are marked for live confirmation, never assumed.
- **C-7 Repo anchors:** all file/line references verified at HEAD `138d83b` (see design.md §2).

## 7. Open decisions (user input required before/at design confirmation)

| ID | Decision | Recommendation (PO position) |
|----|----------|------------------------------|
| D1 | Cache TTL defaults + size cap (clusters/opinions/citations vs dockets) | Opinions/clusters/citations: TTL 30 days, effectively immutable; dockets: TTL 24h (mutable); cap 100 MB total, oldest-first eviction. Cache dir `.cache/court-listener/` (configurable via env). |
| D2 | Search default/cap limits | Default 10 (per FR-2.2), allow explicit 1–50. |
| D3 | Does `batch_lookup_citations` (citation.py:99+) also strip? | Yes — strip candidate clusters with the same FR-3 allowlist; keep counts + per-citation status. Raw pass-through remains only via get-tools opt-out (D4). |
| D4 | In-place breaking change vs. new tools; opt-out param | Modify existing `opinion`/`cluster`/`docket`/search tools in place; add `include_all_fields: bool = False` opt-out on get tools; bump version to 0.2.0. Avoids tool-surface duplication. |
| D5 | NL adapter scope | Implement FR-5's minimal deterministic grammar **and** FR-6 doc improvements (they reinforce each other). Alternative — docs-only, no adapter — was considered and rejected: clients demonstrably paste NL blobs into `q`; the adapter removes that failure mode while the docstrings teach the direct path. |
| D6 | Rate-limit/retry handling | Out of scope (Non-goals). Confirm. |
| D7 | Cache persistence across container rebuilds | Add a bind mount (e.g. `./.data/cache:/app/.cache`) in compose; without it the cache is lost on image rebuilds. Recommend the bind mount. |

## 8. Dependencies

- **User-provided:** `COURT_LISTENER_API_KEY` (live verification only — C-3).
- **Environment:** `registry.localdomain:5000` reachable for image push/pull (user-executed).
- **Upstream:** CourtListener v4 API (https://www.courtlistener.com/api/rest/v4/) — no other external services.

---

**CHECKPOINT 1 — User confirmation required:** confirm FRs 1–6, non-goals, constraints, and open decisions D1–D7 (or amend) before design is finalized into implementation tasks.
