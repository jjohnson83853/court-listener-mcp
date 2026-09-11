# design.md — court-listener-mcp upgrade: exact citation resolution + token efficiency

- **Status:** DRAFT — awaiting user confirmation (SpecDD Checkpoint 2)
- **Depends on:** `spec/requirements.md` (FR-1…FR-6, D1–D7). This document references, not restates.
- **Date:** 2026-09-10 · Repo HEAD `138d83b`

---

## 1. Environment findings

All anchors verified at HEAD `138d83b` (via codebase search; line numbers current):

### 1.1 Code anchors

| Anchor | Location | Fact |
|---|---|---|
| Base URL / API key | `app/config.py:34` (`courtlistener_base_url`), `:50–65` (`get_api_key`, env-first then `.env`), `:68–84` (`get_auth_headers`, `Token <key>`) | Config via pydantic-settings; `.env` fallback (config.py:38–43). **`COURT_LISTENER_API_KEY` currently unset** (requirements C-3). |
| Fetch path | `app/tools/get.py:22–63` `_fetch_resource` | GET `{base}{endpoint}/{id}/`, returns `response.json()` **verbatim** (:56). Tools `opinion` (:66–72, endpoint `opinions`), `docket` (:75–81), `audio` (:84–90), `cluster` (:93–99), `person` (:102–108). |
| Search path | `app/tools/search.py:23–88` `_search_courtlistener` | Builds params `{q, order_by, type, hit=limit}` (:55–63) + truthy filters (:66–68). Raw response returned (:78–81). |
| Search tools | `search.py:92` `opinions` (type `o`; params `court`, `case_name`, `judge`, `filed_after`, `filed_before`, `cited_gt/lt`; default limit 20, cap 100 at :116–118); `dockets` (:141, type `d`, uses `date_filed_after/before`, `docket_number`, `party_name`); `dockets_with_documents` (:186, type `r`); `recap_documents` (:234, type `rd`) | Typed filter params already exist — the NL adapter (FR-5) maps NL onto **these**, not onto a query-string syntax. |
| Citation tools | `app/tools/citation.py:34–96` `lookup_citation` (POST `citation-lookup/` with `data={"text": ...}` at :70–76); `:99–160` `batch_lookup_citations`; `:448–576` `enhanced_citation_lookup` (citeurl parse :492–515 + lookup :518–546, returns counts/URLs only) | No cluster-ID → text flow exists. citeurl used for format validation (:167–216, :323–324, :392–393). |
| Server composition | `app/server.py:21` imports `citation_server, get_server, search_server` from `app.tools`; FastMCP app with lifespan (shared `httpx.AsyncClient`, :39–62); loguru file log with rotation (:69–71) | New tools attach to the **existing three servers** via `@server.tool()` — no new server objects, no `app/tools/__init__.py` server changes beyond what's needed for helper imports. |
| Tests | `tests/conftest.py` (`client` fixture = FastMCP `Client(mcp)`, `slow`/`integration` markers); `tests/test_mocked.py` et al.; dev deps include `respx`, `pytest-asyncio` (pyproject.toml:40–55) | All **[T]** acceptance criteria can be met with respx-mocked httpx + the existing client fixture, no API key. |
| Deployment | `docker-compose.yml`: `court-listener-mcp` service, `build: .` + `image: court-listener-mcp:latest`, HTTP :8000, `env_file: .env`, `restart: unless-stopped`, socket healthcheck; `Dockerfile` present | Persistent service → Watchtower redeploy model (requirements C-5). |

### 1.2 CourtListener v4 API behavior (verified against official docs — FLP wiki "Legal Search API v4" and "Citation Lookup and Verification API v4"; not guessed)

- **Citation-lookup response** (POST `/api/rest/v4/citation-lookup/`): JSON **array**, one entry per citation found: `{citation, normalized_citations[], start_index, end_index, status, error_message, clusters[]}`. Per-citation `status`: `200` resolved (clusters non-empty), `300` ambiguous (multiple clusters), `404` valid-but-not-in-corpus, `400` unknown reporter, `429` over cap (250 citations/request; 64,000-char text cap). Cluster objects carry `id` (= cluster ID), `case_name`, `date_filed`, `citations`, `absolute_url` — but **not** `court` (court hangs off the docket in CourtListener's model).
- **Search response** (GET `/search/`): DRF envelope `{count, next, previous, results[]}`. Opinion-type (`o`) hits use camelCase (`caseName`, `dateFiled`, `citation[]`, `cluster_id`, `docket_id`, `court`, `court_id`, `docketNumber`, `judge`, `citeCount`, `status`) and nest sub-opinions in `opinions[]` where each sub-opinion carries `snippet`. Snippets for types `o`, `r`, `rd`; not `d`. `highlight=on` enables highlighted snippets (HTML5 `<mark>`-style, up to five matches); without it, the first 500 chars of the best text field are returned. Without `q`, snippet shows first 500 chars of `text`.
- **Detail endpoints** (`/clusters/{id}/`, `/opinions/{id}/`, `/dockets/{id}/`) return bare objects with **snake_case** fields (`case_name`, `date_filed`, …). `/opinions/{id}/` carries `cluster` (URL with cluster ID), `plain_text`, `html*` variants; `/clusters/{id}/` carries `citations`, `sub_opinions` (URLs), `docket` (URL). Court on a docket is a FK/URL — see risk R1.

### 1.3 Assumptions (explicitly NOT verified — confirm live per C-3)

- A-1: cluster detail objects expose enough of `case_name`/`citations`/`date_filed`/`sub_opinions` for FR-3's schema (expected from v4 serializer; **R1**).
- A-2: `plain_text` is non-null for the opinions in the primary flows; fallback behavior defined in ADR-3.4 (**R3**).

## 2. Architecture decision record

### ADR-1 — Allowlist stripping + normalization at the tool boundary (new `app/fields.py`)

**Decision.** A pure helper module `app/fields.py` defines per-resource allowlists and `strip_*` functions producing the FR-3.1 normalized schema (stable camelCase output keys regardless of API casing). Every tool boundary (resolution tool, get tools, search hits, lookup candidates) calls it before returning. Internal helpers keep returning raw dicts; stripping happens exactly once, at the tool return.

**Alternatives.** (a) Raw pass-through + client-side filtering — rejected: the client pays the tokens before it can filter (the very problem FR-3 exists to fix). (b) Pydantic response models per resource — rejected for now: more code, and API shape drift would turn successful responses into validation errors (violates NFR-6); allowlists degrade gracefully by omitting missing fields.

**Rationale.** One place to tune the token/coverage tradeoff; casing normalization (search camelCase vs detail snake_case) is a real inconsistency (finding 1.2) that would otherwise leak to clients.

### ADR-2 — File cache in new `app/cache.py`, `.cache/court-listener/`, four namespaces (FR-4)

**Decision.** Simple JSON-file cache, namespaced directories `citations/`, `clusters/`, `dockets/`, `opinions/` under a configurable root (default `.cache/court-listener/`; Config fields `courtlistener_cache_dir`, `courtlistener_cache_ttl_static` (default 30d, clusters/opinions/citations), `courtlistener_cache_ttl_dockets` (default 24h), `courtlistener_cache_max_mb` (default 100, oldest-first eviction by mtime). Entries: `{"cached_at": ts, "data": {...}}`. Atomic writes (temp + `os.replace`). Lookups are sync file reads inside async tools (payloads are small; no thread-offload needed). Search results are never cached.

**Alternatives.** (a) No cache — rejected: FR-1.4/FR-2 "fetch once" depends on it. (b) SQLite — rejected: more machinery for no benefit at this scale; plain files are greppable and disposable (NFR-3). (c) In-process dict — rejected: dies with the server; file cache survives restarts (AC-4.7) and is inspectable.

**TTL rationale.** Opinions/clusters are effectively immutable; dockets accrue entries — hence two TTLs (D1). Defaults are PO recommendations pending user confirmation.

### ADR-3 — Exact citation resolution as a new tool on `citation_server` (FR-1)

**Decision.** New tool `resolve_citation` in `app/tools/citation.py`, flow:

1. Normalize input (optional citeurl pass; on citeurl failure, send the raw string — citation-lookup does its own extraction).
2. Check cache `citations/` (key: canonical/normalized citation string, slugified). Hit → skip to 4 with cached cluster ID.
3. `POST citation-lookup/` (reuse the request shape at citation.py:70–76). Interpret per-citation `status`:
   - `200` → take `clusters[0].id`.
   - `300` → return stripped candidates (fields.py allowlist) + instruction to re-call with the chosen cluster ID. No fetch.
   - `404`/`400` → structured not-found/invalid result.
   - `429` → surfaced, not retried (Non-goals).
4. Fetch cluster detail `GET clusters/{id}/` (cache `clusters/`) → extract metadata (FR-3.1 schema) + `sub_opinions` URLs → fetch opinion detail(s) (cache `opinions/`, typically the lead opinion only by default; `all_opinions: bool = False` param fetches all sub-opinions).
5. Return `{citation, status, cluster: {caseName, citations, court, dateFiled, cluster_id, docket_id}, opinionText: ...}`.

**Court field.** Cluster objects (both lookup and detail) do not carry `court` (finding 1.2 / mcp-courtwatch corroboration). The resolution flow fetches the docket (cached) **only when** `court` is requested-and-missing; the tool docstring says `court` may require one extra cached fetch. If live testing shows clusters do carry court (R1), the docket fetch is skipped.

**Alternatives.** (a) Extend `enhanced_citation_lookup` with a follow-through flag — rejected: that tool's contract is "analyze", not "fetch"; overloading it muddies two use cases. (b) Compose in the client from `lookup_citation` + `cluster` + `opinion` — rejected: that's exactly the multi-call fuzzy chain FR-1 exists to replace (though it remains possible, since the underlying tools stay).

**Text fallback (R3).** `opinionText` prefers `plain_text`; if null, falls back `html_with_citations` → `html` (tags stripped to text via stdlib `html.parser`); if all null, returns the list of sub-opinion IDs with a `text_unavailable` note. Never fatal (NFR-6).

### ADR-4 — Get tools: stripped + cached, in place, with opt-out (FR-3, D4)

**Decision.** `_fetch_resource` (get.py:22–63) gains optional `cache_ns` and keeps returning raw data internally. Tool wrappers `opinion`, `cluster`, `docket` become: cache check → (miss) `_fetch_resource` → cache write → strip via fields.py → return. `opinion` joins its cluster for metadata (cluster URL → ID → cached cluster fetch); `docket` strips per its allowlist. New param `include_all_fields: bool = False` on these three tools returns the raw (cached) payload (D4). `audio`/`person` unchanged (Non-goals). Version bumps to 0.2.0.

**Alternative.** New parallel tools (`opinion_stripped`…) — rejected: doubles the tool surface clients must learn; in-place + opt-out matches NFR-5 and D4.

### ADR-5 — Snippet search: strip hits, default limit 10, `highlight=on` (FR-2)

**Decision.** In `_search_courtlistener` (search.py:23–88):
- Add `highlight=on` to params for types `o`, `r`, `rd` (not `d`).
- After `response.json()` (:78), map `results[]` through `fields.strip_search_hit(hit, search_type)`: allowlist `caseName, citations, court, dateFiled, cluster_id, docket_id, snippet` (+`docketNumber` for `r`/`rd`); snippet extracted from nested `opinions[0].snippet` for `o`-type hits, top-level for `r`/`rd`. Envelope kept: `{count, results: [...stripped...]}` (drop `next`/`previous` cursor URLs — pagination is a non-goal; keep `count`).
- Tool defaults: `limit` default 20 → **10** across the four tools (D2; explicit range 1–50).

**Alternative.** Return snippets only as a list, dropping the envelope — rejected: `count` is cheap and useful for the client to decide whether to refine.

### ADR-6 — NL adapter as a separate deterministic tool (FR-5, D5)

**Decision.** New tool `search_natural_language` on `search_server` (search.py), pure function `parse_natural_query(text) -> NaturalQuery` in a new `app/query.py` (no I/O — trivially unit-testable). Grammar (deterministic, regex + alias table):
- Court aliases: `scotus|supreme court` → `court=scotus`; `ninth circuit|ca9` → `court=ca9`; small table of common federal circuits + a pass-through for known court-ID-looking tokens.
- Dates: `after|since <year|date>` → `filed_after` (year → `YYYY-01-01`); `before|through <year|date>` → `filed_before` (year → `YYYY-12-31`).
- `"quoted phrases"` → quoted `q` terms (AND-joined); bare terms → `q` keywords.
- `judge X` / `justice X` → `judge`; `case X` / `X v. Y` pattern → `case_name`.
- Everything unmapped stays in `q` verbatim (FR-5.4 — never drop intent).

The tool returns `{interpreted_query: {q, court, filed_after, ...}, count, results: [...stripped hits...]}` so the client sees the interpretation (FR-5.3). The tool then calls the existing `_search_courtlistener` path — one search implementation, two front doors.

**PO challenge on record (D5):** the strongest alternative is **no adapter** — LLM clients can fill the typed params directly, and FR-6's docstring improvements teach them how. The adapter exists because clients demonstrably paste NL blobs into `q`, producing retry loops today. It is deliberately minimal: no synonyms beyond the alias table, no fuzzy matching, no network. If it proves to mis-route queries in practice, deleting `app/query.py` + one tool is a clean rollback.

### ADR-7 — Deployment: PR-based, user-built image to `registry.localdomain:5000`, Watchtower pickup (C-4/C-5)

**Decision.** `iac-devops` opens a PR containing code + updated `docker-compose.yml` (image → `registry.localdomain:5000/court-listener-mcp:0.2.0`, plus optional `latest` tag; cache bind mount `./.data/cache:/app/.cache` per D7) and `.env.template` additions (cache TTL/size vars — no secrets). User merges, then builds/pushes locally:

```
docker build -t registry.localdomain:5000/court-listener-mcp:0.2.0 .
docker push registry.localdomain:5000/court-listener-mcp:0.2.0
```

Watchtower (already running in this environment) redeploys the persistent container automatically (C-5). No CI/CD, no agent-driven remote builds (C-4).

## 3. Module design

```
app/
├── config.py            # + cache Config fields (ADR-2): dir, TTLs, max_mb
├── fields.py            # NEW (ADR-1): allowlists, strip_resource/strip_search_hit/
│                        #   strip_candidates, casing normalization
├── cache.py             # NEW (ADR-2): FileCache {get, put, evict}, namespaces, TTL, atomic writes
├── query.py             # NEW (ADR-6): parse_natural_query(text) -> NaturalQuery (pure)
├── server.py            # unchanged composition (server.py:21) — no new servers
└── tools/
    ├── citation.py      # + resolve_citation tool (ADR-3); batch stripping per D3
    ├── get.py           # opinion/cluster/docket: cache+strip+opt-out (ADR-4)
    └── search.py        # hit stripping, highlight=on, limit defaults (ADR-5);
                         # + search_natural_language tool (ADR-6)
tests/
├── test_fields.py       # NEW: AC-3.1–3.3, AC-2.1
├── test_cache.py        # NEW: AC-4.1–4.6
├── test_resolve_citation.py  # NEW: AC-1.1–1.4
├── test_query.py        # NEW: AC-5.1–5.4
├── test_mocked.py       # EXTEND: get/search stripped+cached behavior (AC-2.2–2.4, AC-4.1)
└── test_integration.py  # NEW: [L] live tests, @pytest.mark.integration, skipped w/o key (AC-1.5, 2.5, 4.7, NFR-1)
```

### 3.1 Data flows

**Exact resolution (FR-1):**
```
citation string
  → [cache: citations/<slug>] hit? ─────────────────┐
  ↓ miss                                             │
  POST citation-lookup/  ── status 200 ── cluster_id ┤
       │ status 300 → return stripped candidates    │
       │ status 404/400 → return not-found          │
  ↓                                                  │
  [cache: clusters/cluster-<id>] ─ miss → GET clusters/<id>/ ── cache write
  ↓ (metadata: caseName, citations, dateFiled; court via docket if needed & missing)
  GET sub-opinions (lead only unless all_opinions) → [cache: opinions/opinion-<id>]
  ↓
  fields.strip → {citation, status, cluster{...}, opinionText}
```

**Snippet discovery (FR-2):**
```
NL or fielded query → (optional) app/query.parse_natural_query
  → GET search/?q&court&filed_after&...&highlight=on&hit=10
  → results[] → fields.strip_search_hit → {count, results:[{caseName, citations, court, dateFiled, cluster_id, snippet}]}
client picks cluster_id → cluster/opinion get (cached, stripped) → full text once (FR-2.4)
```

## 4. Threat model & secret handling

| Threat | Handling |
|---|---|
| API key leakage | Key lives in env/`.env` only (config.py:50–65, unchanged); never in cache payloads (AC-4.6), never in tool output; loguru lines log IDs/queries, not headers. `.env` is compose `env_file` — not baked into image (Dockerfile ships no secrets; `.dockerignore` covers `.env` — verify in tasks). |
| Prompt injection via opinion text | Court text is untrusted content returned as tool data (unchanged exposure vs today; stripping doesn't add risk). README note (FR-6.2): treat `opinionText`/`snippet` as quoted material, not instructions. |
| Cache poisoning / tampering | Cache is local single-user files under the service's working dir; no network exposure; disposable (safe `rm -rf` — NFR-3). Corruption handled by treating unreadable/invalid entries as misses (never fatal, NFR-6). |
| Unbounded disk growth | Size cap + oldest-first eviction (ADR-2, D1). |
| Data exposure | Cached content is public court records; no PII beyond what the API already returns. |

## 5. Monitoring & failure notification

No new monitoring stack (NFR-3). Existing loguru file logging (server.py:69–71) gains structured `ctx.info` lines: `cache hit/miss (<ns>/<key>)`, `resolved citation <x> → cluster <id>`, `stripped <n> fields`. A stuck/broken cache self-heals (miss → refetch); a broken registry/Watchtower deploy is out of this server's blast radius.

## 6. Design risks (tracked, with owners in tasks.md)

| ID | Risk | Mitigation |
|---|---|---|
| R1 | Cluster/detail field names or `court` availability differ from A-1 (e.g., snake_case set differs) — would change FR-1.3 join depth | fields.py casing map is data-driven; **confirm live** once the key exists (integration test asserts schema on one real cluster); docket-fetch fallback already designed (ADR-3). |
| R2 | Snippet nesting: `o`-type hits nest snippets in `opinions[]`; multi-opinion clusters → which snippet? | Take the lead/first sub-opinion's snippet; documented in fields.py docstring; covered by fixture test (AC-2.1). |
| R3 | `plain_text` null on some opinions (scanned PDFs) | Fallback chain + `text_unavailable` note (ADR-3); never fatal. |
| R4 | v4 API drift (docs note fields get removed across minor versions) | Allowlist stripping degrades gracefully (missing → omitted, NFR-6); integration tests catch drift at upgrade time. |
| R5 | `.dockerignore` may not exclude `.cache/` → cache baked into images | Task verifies/patches `.dockerignore` and `.gitignore`. |

## 7. Storage & backup

- Cache: disposable, rebuildable from API; no backup needed. Bind mount `./.data/cache` (D7) keeps it across container recreation.
- No other new persistent state. Existing logs rotation unchanged.

---

**CHECKPOINT 2 — User confirmation required:** confirm ADRs 1–7 (especially D1/D3/D4/D5/D7 dispositions embedded above: TTL defaults, batch stripping, in-place change + opt-out, adapter scope, cache bind mount) before tasks.md is finalized for handoff.
