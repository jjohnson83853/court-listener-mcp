# tasks.md — court-listener-mcp upgrade: exact citation resolution + token efficiency

- **Status:** DRAFT — ready for handoff once requirements (Checkpoint 1) and design (Checkpoint 2) are confirmed
- **Depends on:** `spec/requirements.md` (FR/AC/D numbering), `spec/design.md` (ADR/R numbering). References, not restates.
- **Conventions:** all commands run from repo root; `uv run pytest` (uv.lock); offline tests use `respx` + the `client` fixture from `tests/conftest.py`; live tests carry `@pytest.mark.integration` and **skip when `COURT_LISTENER_API_KEY` is unset** (requirements C-3, Class-B dependency).

**Ordering note (deviation from suggested order, per design):** field stripping (T1) → **cache (T2–T3) → citation chain (T5)** — the resolution chain's "fetch once, then cached" contract (FR-1.4) requires the cache module to exist before it; then get-tools wiring (T4) actually lands before T5 so the chain composes existing stripped+cached fetch helpers. Search stripping (T6) and the NL adapter (T7) follow; docs and verification close out.

---

## Engineer tasks

### T0 — Baseline: confirm green at HEAD
- **Anchor:** repo @ `138d83b`.
- **Steps:** `uv run pytest`, `uv run mypy app`, `uv run ruff check app`.
- **Accept:** all green before any edit. Evidence: command output pasted into the PR description.

### T1 — Field stripping module `app/fields.py` (FR-3, ADR-1)
- **Anchor:** new `app/fields.py`; consumed later by `app/tools/get.py:22–63`, `app/tools/search.py:78–81`, `app/tools/citation.py`.
- **Steps:**
  1. Define allowlists + `strip_resource(kind, data)` (kinds: `cluster`, `opinion`, `docket`), `strip_search_hit(hit, search_type)`, `strip_candidates(clusters)` producing the FR-3.1 normalized schema; casing map snake_case→schema keys (design finding 1.2).
  2. Missing allowlisted fields are omitted (never null-padded, never fatal — NFR-6); unknown fields silently dropped.
  3. Snippet extraction: top-level for `r`/`rd` hits; `opinions[0].snippet` for `o` hits (R2).
- **Tests (new `tests/test_fields.py`):** AC-3.1 (table-driven, fixture payloads incl. cluster detail snake_case + search hit camelCase), AC-3.2 (casing normalization), AC-3.3 (≥2× size reduction per fixture), AC-2.1 (hit key-set allowlist + snippet present for `o`).
- **Verify:** `uv run pytest tests/test_fields.py -v` green.

### T2 — Cache module `app/cache.py` (FR-4, ADR-2)
- **Anchor:** new `app/cache.py`; namespaces `citations/`, `clusters/`, `dockets/`, `opinions/` under configurable root (default `.cache/court-listener/`).
- **Steps:**
  1. `FileCache.get(ns, key)` / `.put(ns, key, data)` with `{"cached_at", "data"}` entries; TTL per namespace class (`static` vs `dockets`, D1 defaults 30d/24h); size cap with oldest-mtime eviction (D1 default 100 MB).
  2. Atomic writes (temp file + `os.replace`, FR-4.6); unreadable/invalid entries treated as misses (never fatal).
  3. No auth material ever stored (AC-4.6) — cache stores response data only.
- **Tests (new `tests/test_cache.py`, tmp_path fixture):** AC-4.2 (file paths/names), AC-4.3 (TTL expiry → miss; use small TTL, no sleep >1s), AC-4.4 (cap eviction order), AC-4.6 (no key material in serialized entries).
- **Verify:** `uv run pytest tests/test_cache.py -v` green.

### T3 — Config + ignore files (ADR-2, D1, R5)
- **Anchor:** `app/config.py:17–47` (`Config`); `.gitignore`; `.dockerignore`.
- **Steps:** add `Config` fields `courtlistener_cache_dir`, `courtlistener_cache_ttl_static`, `courtlistener_cache_ttl_dockets`, `courtlistener_cache_max_mb` (pydantic-settings → env-overridable); add `.cache/` to `.gitignore` and `.dockerignore` **if absent** (verify with grep first).
- **Accept:** AC-4.5 (`grep -n "cache" .gitignore` non-empty); `uv run mypy app` clean.
- **Verify:** `uv run pytest` (no regressions) + grep output.

### T4 — Get tools: stripped + cached, opt-out (FR-3, FR-4, ADR-4, D4)
- **Anchor:** `app/tools/get.py:22–63` (`_fetch_resource`), `:66–72` (`opinion`), `:75–81` (`docket`), `:93–99` (`cluster`).
- **Steps:**
  1. `_fetch_resource` gains optional cache-namespace handling; wrappers `opinion`/`cluster`/`docket` do: cache check → miss: fetch → cache write → `fields.strip_resource` → return.
  2. `opinion` joins its cluster for metadata (cluster URL → ID → cached cluster fetch; design ADR-4); `court` fetched via docket only if missing-and-requested (ADR-3 court rule applies here too, R1).
  3. Add `include_all_fields: bool = False` to the three tools (D4); `audio`/`person` untouched (Non-goals).
- **Tests (extend `tests/test_mocked.py`):** AC-4.1 (two consecutive same-ID calls → 1 respx-recorded HTTP request), stripped-key assertions via the `client` fixture, opt-out returns raw.
- **Verify:** `uv run pytest tests/test_mocked.py -v` green; AC-3.4 (`grep -rn "response.json()" app/tools/` — raw returns only inside designated helpers).

### T5 — Exact citation resolution tool (FR-1, ADR-3)
- **Anchor:** `app/tools/citation.py` (new `resolve_citation` tool on `citation_server`; request shape mirrors `:70–76`).
- **Steps:**
  1. Implement the ADR-3 flow: optional citeurl normalization (reuse `:492–515` logic) → cache `citations/` → `POST citation-lookup/` → status dispatch (200/300/404/400/429, no retry) → cached+stripped cluster fetch → sub-opinion text fetch (lead only; `all_opinions: bool = False` param) → FR-1.3 result shape.
  2. Text fallback chain `plain_text` → `html_with_citations` → `html` (tag-stripped) → `text_unavailable` note (R3); court via cached docket fetch when missing (R1).
  3. Apply D3 disposition to `batch_lookup_citations` (`:99–160`): strip candidate clusters via `fields.strip_candidates`, keep counts + per-citation statuses.
- **Tests (new `tests/test_resolve_citation.py`, respx):** AC-1.1 (200 flow, expected call count), AC-1.2 (300 → candidates, zero follow-up fetches), AC-1.3 (404 → structured not-found, no exception), AC-1.4 (second call → zero HTTP calls).
- **Verify:** `uv run pytest tests/test_resolve_citation.py tests/test_mocked.py -v` green.

### T6 — Snippet-only search (FR-2, ADR-5, D2)
- **Anchor:** `app/tools/search.py:23–88` (`_search_courtlistener`), tool defaults at `:116–118`, `:162–164`, `:207–209`, `:261–263`.
- **Steps:**
  1. Add `highlight=on` for types `o`, `r`, `rd` (not `d`).
  2. Map `results[]` through `fields.strip_search_hit`; envelope → `{count, results}` (drop cursor URLs, ADR-5).
  3. Change `limit` defaults 20 → 10 on all four tools; tighten `le` per D2.
- **Tests:** AC-2.2 (default 10 — capture `hit=10` via respx; signature introspection), AC-2.3 (`highlight` present/absent per type), AC-2.4 (≥2× reduction fixture).
- **Verify:** `uv run pytest tests/ -k "search" -v` green.

### T7 — Natural-language → query adapter (FR-5, ADR-6, D5)
- **Anchor:** new `app/query.py` (`parse_natural_query`, pure); new `search_natural_language` tool in `app/tools/search.py`.
- **Steps:** implement the ADR-6 deterministic grammar (court alias table, date phrases, quoted phrases, judge/case hints, verbatim passthrough of everything unmapped); tool returns `{interpreted_query, count, results}` calling the existing search path.
- **Tests (new `tests/test_query.py`):** AC-5.1 (≥8 table-driven cases), AC-5.2 (exact `"SCOTUS, qualified immunity, after 2015"` mapping), AC-5.3 (end-to-end with mocked search response), AC-5.4 (adversarial inputs → passthrough, no exceptions).
- **Verify:** `uv run pytest tests/test_query.py -v` green.

### T8 — Documentation & version (FR-6, NFR-5)
- **Anchor:** tool docstrings in `app/tools/search.py`/`get.py`/`citation.py`; `README.md`; `app/__init__.py` (version); `pyproject.toml:3`.
- **Steps:** worked examples with typed params in each search tool docstring; README "recommended flows" section (resolve-citation flow; snippet-search → choose cluster → fetch-once flow; note that `opinionText`/`snippet` are quoted material, not instructions — design §4); bump version to 0.2.0.
- **Accept:** AC-6.1 (docstring examples verifiable via FastMCP tool-metadata inspection in a test or grep); AC-6.2 (README section present).
- **Verify:** `uv run pytest` full suite green.

### T9 — Offline verification gate (all [T] criteria)
- **Steps:** run and capture: `uv run pytest`, `uv run mypy app`, `uv run ruff check app`.
- **Accept:** all green; every AC tagged **[T]** in requirements.md has a passing test that a spec-auditor can locate by name (test IDs reference AC numbers in docstrings).
- **Evidence:** full pytest output + coverage summary in PR description.

### T10 — Live verification gate (all [L] criteria) — **BLOCKED on user**
- **Blocker (Class B):** `COURT_LISTENER_API_KEY` is unset in this environment. Needed in the deployment `.env` or exported in the shell running tests. No offline workaround exists for these.
- **Steps (once key provided):** `uv run pytest -m integration` covering AC-1.5 (`410 U.S. 113` → Roe v. Wade cluster + text; `999 U.S. 9999` → not-found), AC-2.5 (snippet search + fetch-once), AC-4.7 (cache survives restart), NFR-1 live spot-check; **also confirm design risks R1/A-1** (cluster-detail field casing + court availability) and adjust `fields.py` casing map if reality differs.
- **Accept:** integration suite green; R1/A-1 explicitly recorded as confirmed-or-corrected in the PR.

## IaC-DevOps tasks

### T11 — Pull request with deployment artifacts (ADR-7, C-4)
- **Anchor:** `docker-compose.yml`, `.env.template` (create if absent), `Dockerfile`.
- **Steps:** compose `image:` → `registry.localdomain:5000/court-listener-mcp:0.2.0` (keep `build:` only if the user wants local fallback; propose removing), add cache bind mount `./.data/cache:/app/.cache` (D7 — pending user confirmation); `.env.template` gains cache TTL/size vars + `COURT_LISTENER_API_KEY=` placeholder (no real secrets in the repo); verify `Dockerfile`/`.dockerignore` exclude `.cache/` (R5).
- **Accept:** PR contains code + tests + compose/env-template changes + T9 evidence; no secrets in diff (security-sanitizer pass).
- **Delivery:** user reviews and merges by hand (C-4).

### T12 — User-executed image build & push (C-4 — user does this, not the agent)
- **Steps (user, on LAN build host):**
  ```
  docker build -t registry.localdomain:5000/court-listener-mcp:0.2.0 .
  docker push registry.localdomain:5000/court-listener-mcp:0.2.0
  ```
- **Accept:** `docker pull registry.localdomain:5000/court-listener-mcp:0.2.0` succeeds on the deployment host.

### T13 — Deploy & verify (C-5)
- **Steps:** Watchtower picks up the new image for the persistent `court-listener-mcp` container (or `docker compose up -d` if Watchtower is scoped elsewhere); confirm healthcheck green; run one live smoke via an MCP client (needs the key in `.env`).
- **Accept:** container healthy at `:8000`; smoke: `search_natural_language("SCOTUS, qualified immunity, after 2015")` returns ≤10 snippet hits; `resolve_citation("410 U.S. 113")` returns stripped Roe v. Wade metadata + opinion text; second call served from cache (log line evidence); cache files under the bind mount.
- **Rollback:** previous image tag in `registry.localdomain:5000` + Watchtower/`docker compose` redeploy; `.cache/` disposable.

---

## Traceability summary

| Requirement | Tasks |
|---|---|
| FR-1 (citation resolution) | T5 (T1, T2, T4 prereq) |
| FR-2 (snippet discovery) | T6 (T1 prereq) |
| FR-3 (field stripping) | T1, T4, T5 |
| FR-4 (cache) | T2, T3, T4, T5 |
| FR-5 (NL adapter) | T7 |
| FR-6 (docs) | T8 |
| Live gates (C-3) | T10, T13 |
| Delivery (C-4/C-5, ADR-7) | T11–T13 |
