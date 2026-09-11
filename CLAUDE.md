# dns-switcher

## Purpose

`dns-switcher` is a standalone service that manages Cloudflare DNS records for
two domains (and their subdomains) and lets an operator switch which
datacenter's IP a record points to — either one record at a time, or in bulk
groups. It is called internally, over HTTP, by a Telegram bot ("AloBot")
running in a sibling Docker container. It is not exposed publicly.

## Architecture

```
Telegram user → AloBot (dns module/router)
                   │  internal REST call, shared Docker network,
                   │  authenticated via INTERNAL_API_SHARED_SECRET header
                   ▼
        dns-switcher service (this project, FastAPI)
                   │
        PostgreSQL (datacenters / domains / dns_targets / switch_groups / audit_log)
                   │
        Cloudflare API (Zone:DNS:Edit token, scoped to 2 zones)
```

Stack: Python 3.12, FastAPI + uvicorn, SQLAlchemy 2.x (async) + Alembic,
httpx for the Cloudflare client, Pydantic v2 for schemas, pytest +
pytest-asyncio for tests. Deployed as its own container via
docker-compose, separate from any other service on the host.

## Data model

- **Datacenter**: `id`, `name` (unique), `status` (active | standby |
  disabled), `notes`, `ip_address` (nullable — added in Phase 2; see below)
- **Domain**: `id`, `name` (e.g. `example.com`), `cloudflare_zone_id`
- **DnsTarget**: `id`, `domain_id` (FK), `name` (subdomain or `@` for root),
  `record_type` (A | AAAA | CNAME), `cloudflare_record_id` (nullable until
  first sync), `current_datacenter_id` (FK, nullable), `proxied` (bool)
- **SwitchGroup**: `id`, `name`, `description` — a named, ordered set of
  DnsTarget rows that get switched together in one bulk action
- **SwitchGroupMember**: `switch_group_id` (FK), `dns_target_id` (FK) — join
  table
- **AuditLogEntry**: `id`, `created_at`, `actor` (Telegram user id/username),
  `action_type` (single | bulk | rollback), `dns_target_id` (nullable FK),
  `switch_group_id` (nullable FK), `previous_datacenter_id` (nullable FK —
  see Phase 3 note below), `new_datacenter_id`, `cloudflare_record_id`,
  `status` (success | failed | rolled_back), `error_message` (nullable),
  `rollback_of_id` (nullable, self-referential FK — added in Phase 3)

Deleting a Datacenter that is still referenced (by a DnsTarget or an
AuditLogEntry) must be restricted, not cascaded — history and current
pointers must never silently lose their target.

This model is implemented starting in Phase 1; this scaffold (Phase 0) has
no models yet.

`Datacenter.ip_address` was added in Phase 2, not Phase 1: reconciling what
Cloudflare actually reports for a record against our notion of "which
datacenter is this pointing at" requires knowing each datacenter's IP, and
the original Phase 1 field list omitted it. It's nullable since a
Datacenter can exist before its IP is known.

Two more fixes landed in Phase 3, once the switch/rollback logic exposed
gaps in the Phase 1 field list:

- `previous_datacenter_id` was originally `NOT NULL`, but a DnsTarget's
  very first switch has no prior datacenter to record (it may never have
  been assigned one) — it's nullable now.
- `rollback_of_id` (nullable, FK to `audit_log_entries.id`, `ON DELETE SET
  NULL`) was added so a rollback's audit entry can reference the original
  entry it's undoing, per the explicit requirement that rollback "logs a
  new AuditLogEntry that references the original entry" — there was no
  field for that link before.

## Cloudflare rate limits — respect these everywhere

- **1200 requests / 5 minutes** per API token (user-level).
- **200 requests / second** per source IP.
- Always batch bulk operations with a small delay between calls (see
  `switch_service` in later phases — default ~300ms between DNS updates in a
  bulk switch).
- Back off on HTTP 429: honor the `Retry-After` header if present, otherwise
  use exponential backoff. Never busy-loop retry.

## Golden rule: no blind writes

**No DNS write ever happens without first computing and logging a dry-run
diff** (current record content → proposed new content). Every switch
operation — single or bulk — must go through a `plan_*` step that produces
this diff before any `execute_*` step is allowed to call the Cloudflare API.
This applies even when `execute_*` is called directly: it re-derives the plan
internally rather than trusting a caller-supplied target. The diff is what
gets shown to the operator (via AloBot) for confirmation, and it is what gets
recorded in the audit log regardless of whether the write succeeds.

## Configuration

Environment variables (see `.env.example`):

- `CLOUDFLARE_API_TOKEN` — Cloudflare API token, Zone:DNS:Edit scope on the
  two managed zones. Never log this value.
- `DATABASE_URL` — async SQLAlchemy URL, e.g.
  `postgresql+asyncpg://user:pass@postgres:5432/dns_switcher`.
- `INTERNAL_API_SHARED_SECRET` — shared secret checked against a header on
  every request from the AloBot container. This API has no other auth layer
  and must never be exposed outside the internal Docker network —
  `docker-compose.yml` enforces this by not publishing a host port for the
  `dns-switcher` service at all; reach it only from other containers on
  `dns-switcher-net`, or via `docker compose exec` for local debugging (see
  `RUNBOOK.md`).

## Project layout

```
app/
  api/         FastAPI routers
  core/        config, settings
  db/          SQLAlchemy engine/session setup, declarative base
  models/      ORM models
  schemas/     Pydantic request/response schemas
  services/    business logic (sync, switch, rollback)
  cloudflare/  Cloudflare API client
alembic/       migrations
tests/
```

## ON DELETE behavior

Beyond the explicit rule that deleting a referenced Datacenter must be
restricted, the other foreign keys follow this reasoning:

- `dns_targets.domain_id → domains.id`: **RESTRICT**. A Domain going away
  should never silently wipe out its DnsTargets (and their audit trail);
  targets must be removed explicitly first.
- `dns_targets.current_datacenter_id → datacenters.id`: **RESTRICT** (the
  explicit rule).
- `audit_log_entries.previous_datacenter_id` / `.new_datacenter_id →
  datacenters.id`: **RESTRICT** — audit history must never lose which
  datacenter was involved in a past switch.
- `switch_group_members.switch_group_id → switch_groups.id`: **CASCADE**.
  Deleting a group just disbands it; the membership rows are structural,
  not data worth preserving on their own.
- `switch_group_members.dns_target_id → dns_targets.id`: **CASCADE**, same
  reasoning — a deleted target can't remain a group member.
- `audit_log_entries.dns_target_id → dns_targets.id` and
  `audit_log_entries.switch_group_id → switch_groups.id`: **SET NULL**
  (both columns are nullable for exactly this reason) — the audit row
  survives deletion of the target/group it once referenced.

## Testing

Model and integration tests need a real Postgres (SQLite can't enforce the
native enum types or the RESTRICT/CASCADE/SET NULL behavior these tests
verify). `docker-compose.yml`'s `postgres` service never publishes a host
port (by design — see its own comment), so for local test runs spin up a
throwaway one that does, e.g.
`docker run -d --rm -e POSTGRES_USER=dns_switcher -e POSTGRES_PASSWORD=dns_switcher -e POSTGRES_DB=dns_switcher -p 5432:5432 postgres:16-alpine`,
create the test database once (`CREATE DATABASE dns_switcher_test;`), and
point `TEST_DATABASE_URL` at
`postgresql+asyncpg://dns_switcher:dns_switcher@localhost:5432/dns_switcher_test`.
Each test gets a fresh schema via `create_all`/`drop_all` in
`tests/conftest.py`.

## Status

Phase 0: project structure, config, DB session wiring, and a `/health`
endpoint that reports DB connectivity.

Phase 1: SQLAlchemy models for the full data model (`app/models/`), the
initial Alembic migration (verified with a real upgrade → downgrade →
upgrade cycle against Postgres, including explicit `DROP TYPE` for the
Postgres enums in `downgrade()`, which autogenerate does not emit on its
own), `scripts/seed.py` for upserting domains/datacenters from a YAML file
or interactively, and model-level tests in `tests/test_models.py` covering
every constraint and cascade path above. No business logic yet — Cloudflare
sync and switch operations are Phase 2+.

Phase 2: `app/cloudflare/client.py` — an async Cloudflare API v4 client
(`list_dns_records`, `get_dns_record`, `update_dns_record`,
`create_dns_record`) that retries on HTTP 429 (honoring `Retry-After`) and
5xx with backoff via `tenacity`, and raises `CloudflareApiError` (with the
raw Cloudflare error payload attached) for anything else or once retries
are exhausted. Also `app/services/sync_service.py`'s `SyncService`, which
reconciles a Domain's DnsTargets against Cloudflare: fills in missing
`cloudflare_record_id` by matching name+type, and flags (without
auto-fixing) any DnsTarget whose `current_datacenter_id` doesn't match the
datacenter that record's `content` resolves to via `ip_address`. Both are
covered by respx-mocked tests (`tests/test_cloudflare_client.py`,
`tests/test_sync_service.py`) including the 429-retry and
permanent-failure paths.

Phase 3: `app/services/switch_service.py`'s `SwitchService` — single and
bulk switching with dry-run plans and rollback. `plan_single_switch` never
calls Cloudflare (pure DB diff: current datacenter's `ip_address` → target
datacenter's `ip_address`); `execute_single_switch` always re-derives its
own plan internally (never trusts a caller-supplied diff), then checks
Cloudflare's *live* record content before writing — an already-correct
record is a no-op that's still logged. `execute_bulk_switch` runs targets
sequentially with `BULK_SWITCH_DELAY_SECONDS` (default 0.3s) between
Cloudflare calls, continuing past a per-target failure and returning a
succeeded/failed summary. `rollback` re-executes a switch back to
`previous_datacenter_id`, links the new entry via `rollback_of_id`, and
flips the original entry's status to `rolled_back` only once the reversal
actually succeeds.

The internal API lives in `app/api/switch.py`, guarded by
`verify_internal_secret` (checks `X-Internal-Secret` against
`INTERNAL_API_SHARED_SECRET` with a constant-time comparison; missing and
wrong values both come back as a plain 401, not FastAPI's default 422 for
a missing header) on every route. `app/db/session.get_db` now
commits on a clean request and rolls back on any exception, so routes
don't need to call `session.commit()` themselves — a Cloudflare failure
inside `execute_single_switch` is caught and logged internally rather than
raised, so its `AuditLogEntry(status=failed)` still commits normally.
`GET /datacenters` was added beyond the phase's original endpoint list —
without it AloBot has no way to know which datacenters exist to offer as
switch targets. `GET /domains` was added the same way in Phase 4, once
AloBot's single-switch flow ("pick a domain → list its DnsTargets") turned
out to have no way to list domains at all — `DomainOut` already existed in
`app/schemas/switch.py` from Phase 3 but had never been wired to a route.

Covered by `tests/test_switch_service.py` (plan/execute/rollback, the
skip-when-already-correct idempotency path, bulk partial-failure
tolerance) and `tests/test_switch_api.py` (auth, and the same flows
through the actual HTTP router) — 43 tests passing in total. Verified
end-to-end in the real docker-compose stack: all three migrations apply in
sequence, and `/switch/single/plan` against manually-inserted data
produces the correct diff with zero Cloudflare calls.

Phase 4 (built in the sibling AloBot repo, not here): added `GET /domains`
to this service's API — see the note above — while wiring up the Telegram
bot's "مدیریت DNS" module.

Phase 5: deployment finalized. `docker-compose.yml` no longer publishes a
host port for `dns-switcher` (was `8000:8000`) — that contradicted this
file's own "not exposed publicly" rule, since anyone reaching the host's
port 8000 could hit the API, with only the shared secret (not network
isolation) as defense. AloBot's `docker-compose.yml` (sibling repo) joins
`dns-switcher-net` as an external network so it can still reach this
service by container name. Added `scripts/sync.py` — there was no way to
actually run "the initial Cloudflare sync" the runbook needed to describe,
since `SyncService` (Phase 2) had never been wired to a CLI entrypoint or
API route. See `RUNBOOK.md` for first-time setup, token rotation, reading
the audit log directly from Postgres, and rolling back a change manually
via the API when Telegram is unreachable — all verified against the real
containers, including AloBot successfully reaching `dns-switcher` by name
across the shared network.
