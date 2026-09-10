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
  `switch_group_id` (nullable FK), `previous_datacenter_id`,
  `new_datacenter_id`, `cloudflare_record_id`, `status` (success | failed |
  rolled_back), `error_message` (nullable)

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
  `postgresql+asyncpg://user:pass@db:5432/dns_switcher`.
- `INTERNAL_API_SHARED_SECRET` — shared secret checked against a header on
  every request from the AloBot container. This API has no other auth layer
  and must never be exposed outside the internal Docker network.

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
verify). Point `TEST_DATABASE_URL` at a scratch database — e.g. run
`docker compose up -d db` and use
`postgresql+asyncpg://dns_switcher:dns_switcher@localhost:5432/dns_switcher_test`
(create that database once with `CREATE DATABASE dns_switcher_test;`, since
`docker-compose.yml`'s `db` service doesn't publish 5432 to the host by
default). Each test gets a fresh schema via `create_all`/`drop_all` in
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
