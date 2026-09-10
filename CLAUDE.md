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
  disabled), `notes`
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

## Status

Phase 0 (this scaffold): project structure, config, DB session wiring, and a
`/health` endpoint that reports DB connectivity. No business logic yet.
