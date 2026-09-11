# Security Review — court-listener-mcp (Stage 4, network-exposed product)

- **Scope:** HEAD `138d83b` + uncommitted implementation diff (5013 lines, `/tmp/opencode/court-listener-mcp-review.diff`), deployment files as in tree, planned Stage 5 compose change.
- **Date:** 2026-09-10 · **Reviewer:** security review (Stage 4, mandatory gate)
- **Exposure posture (accepted per spec):** FastMCP streamable HTTP on :8000, trusted LAN, **no MCP AuthN/AuthZ** — confirmed spec non-goal (requirements.md §5). Outbound-only to `https://www.courtlistener.com/api/rest/v4/`.

---

## VERDICT: NO HARD BLOCKERS

No secrets in the diff, no key-leakage path, no execution of untrusted content, no exposure beyond the accepted trusted-LAN posture.

---

## 1. Hard blockers

**None.** Checked for each hard-blocker class:

- **Unauthenticated exposure beyond accepted posture:** none introduced. The diff touches no listener/bind/port code. `docker-compose.yml` / `Dockerfile` are byte-identical to HEAD (absent from the diff entirely — verified against the 5013-line diff; only `app/`, `tests/`, `README.md`, `pyproject.toml`, `uv.lock`, `spec/`, `spec-audit-report.md` appear). The `0.0.0.0` bind (Dockerfile:61, compose `8000:8000`) is the pre-existing accepted posture.
- **Secrets reachable from untrusted segments:** no. `COURT_LISTENER_API_KEY` handling (config.py env/.env) is untouched by the diff; `.dockerignore` still excludes `.env`/`.env.*` (lines 58–59) and `.cache/` (line 77); compose still injects secrets only via `env_file`.
- **Key-leakage paths:** none. Cache stores only tool-supplied response data (see §3); logging logs IDs/queries, never headers; tool outputs never echo auth material. AC-4.6 tests assert `"Authorization"` / `"Token "` / dummy-key absence on serialized cache content with a key deliberately present in the environment — a meaningful (not vacuous) assertion.
- **Executing untrusted content:** none. `fields.py` parses HTML with stdlib `html.parser` for text extraction only; no `eval`/`exec`/`subprocess`/template rendering anywhere in the diff. Untrusted opinion/snippet text flows strictly as data.

## 2. Cache security (FR-4, ADR-2)

- **No auth material:** meaningful assertion confirmed. `tests/test_cache.py::test_ac_4_6_no_key_material_in_entries` and `tests/test_mocked.py::test_ac_4_6_no_key_material_in_cache_after_flow` assert the dummy key (set autouse in `conftest._offline_api_key`) plus `Authorization` / `Token ` substrings are absent from every serialized cache file — and the autouse fixture guarantees a key-like string IS present during the flow, so leakage would actually be caught. End-to-end flow test included.
- **Directory permissions:** root `0700` (`cache.py` `_ensure_root` → `os.chmod(self.root, 0o700)`), files `0600` (chmod on temp file before `os.replace`). Namespace subdirectories are created with default perms but sit under a `0700` root, so unreachable to others. Asserted by `test_directory_permissions_user_only`. Container runs as non-root `courtlistener` user (Dockerfile:44) — perms apply to that user, consistent.
- **Poisoning/tampering posture:** local single-user, disposable (`rm -rf` safe), no network exposure. Corrupt/malformed entries are misses (never fatal). **Note (non-blocking):** cache integrity is unauthenticated (no HMAC) — anything with write access to the cache dir can poison served payloads, including injecting instruction-like text into LLM context via `opinionText`. On a trusted-LAN single-operator box this matches the accepted posture (owner-controlled files, 0700/0600); flagged for awareness, not blocked. If the planned `./.data/cache` bind mount lands on a multi-user host, re-check host-side dir ownership.
- **One non-obvious check:** cache lookup key for citations is a slug of the raw citation (`slugify_key`); cache file *filenames* expose the queried citation strings to anyone who can read the dir — fine for a court-records cache, no sensitive-user data.

## 3. Secret hygiene in the diff

- Grep-style pass over the full 5013-line diff: the only key-shaped string is the test fixture dummy `"test-key-offline-dummy"` (conftest + assertions), correctly used in place of a real key. No tokens, passwords, private IPs, `.env` contents, or real key fragments in code, tests, fixtures, README, or spec docs. Registry name `registry.localdomain:5000` appears only in spec prose (no secrets).
- `pyproject.toml`/`uv.lock` diffs are version-bump only (`0.1.0` → `0.2.0`) — no dependency drift.

## 4. Untrusted-content flow (prompt injection)

- README gains the required note (diff lines 49–53): `opinionText`/`snippet` are quoted court-record material, never instructions.
- New code paths (`resolve_citation`, `search_natural_language`, field stripping, cache) treat API content purely as data; `search_natural_language` is a deterministic regex/alias parser with no LLM and no network during interpretation; adversarial-input tests (AC-5.4, 16 cases) confirm no input raises.
- Snippets from `highlight=on` may contain `<mark>` HTML — returned as data only; no rendering path. Pre-existing `include_all_fields=true` opt-out returns raw API payloads — same-trusted-client surface, unchanged posture.

## 5. Deployment files (unchanged-by-diff claim) — VERIFIED

`docker-compose.yml`, `Dockerfile`, `.dockerignore`, `.gitignore` are absent from the diff. Tree inspection confirms:
- Compose: `env_file: .env` (secrets not baked into image), `restart: unless-stopped`, bridge network, `0.0.0.0:8000` (pre-existing accepted).
- Dockerfile: multi-stage, non-root user, no secrets in layers.
- `.dockerignore`: `.env`, `.env.*`, `.envrc`, `.cache/` all present.

## 6. Planned Stage 5 compose change (as-designed)

| Change | Assessment |
|---|---|
| `image: registry.localdomain:5000/court-listener-mcp:0.2.0` | Good: pinned version tag (not `latest`) → controlled Watchtower rollouts. LAN plain-HTTP registry is a homelab-wide known issue (non-blocking, pre-existing). |
| Bind mount `./.data/cache:/app/.cache` | Sound as-designed. Non-root container user must own the host dir — create it with `chown` to the container UID (or let the app's `0700` chmod on first put succeed only if writable). Also note: with a host-owned dir the app's `chmod 0700` on the root may be skipped if it already exists (`_ensure_root` only chmods on create) — harmless, but pre-create with correct ownership. iac-devops note for T11. |
| Proposed removal of `build:` | Fine; keeps `latest`-tag drift out. |
| `.env.template` cache vars | No secrets planned — confirm placeholder only at PR time. |

## 7. Non-blocking findings

1. **Host port publish `8000:8000` binds all interfaces** — pre-existing accepted trusted-LAN posture; recommend later narrowing to the LAN alias (`"<lan-ip>:8000:8000"`) or dropping `ports:` in favor of the docker network only. (iac-devops, backlog)
2. **Unauthenticated MCP on trusted LAN** — confirmed spec non-goal; logged, not blocked.
3. **Cache entries have no integrity tag (HMAC)** — poisoning requires local file-write access to a 0700 dir; acceptable single-user. Revisit if the cache dir is ever shared/mounted elsewhere.
4. **Executable-bit flips** on `app/__main__.py`, `app/config.py`, `app/server.py` (100644→100755) — likely unintended churn; cosmetic.
5. **`lookup_citation` / `enhanced_citation_lookup` still return raw lookup payloads** — conforms to the FR-3.2 stripped-path list; noted by the spec-audit as a D3-phrasing tension. PO follow-up, not security.
6. **LAN plain-HTTP registry (`registry.localdomain:5000`)** and **Watchtower auto-redeploy** — homelab-wide accepted posture; image tag pinning (0.2.0) mitigates surprise rollbacks.
7. **README Flow-1 tool name** says `resolve_citation`; client-visible name is `citation_resolve_citation` — docs-only; prevents operator confusion during T13 smoke.

## 8. Confirmations requested by the review charter

- ✅ **No secrets in diff** — verified (only the intentional offline dummy key).
- ✅ **Cache/log no-auth-material verified** — AC-4.6 assertions exist at three levels (cache unit, end-to-end mocked flow, dummy-key-present environment) and are meaningful.
- ✅ **Untrusted-content handling documented** — README note present; no new code executes or follows API-sourced content.
- ✅ **Compose/Dockerfile/.dockerignore unchanged by diff** — verified.
- ✅ **Planned Stage 5 change** assessed in §5 — no blocking concerns; one ownership/permission pre-create note for iac-devops.

**Route-back:** none. No bounded rework cycles required. Non-blocking items are handed to iac-devops as backlog notes for T11/T13.
