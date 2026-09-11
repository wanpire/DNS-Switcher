# RUNBOOK

Operational procedures for dns-switcher. See `CLAUDE.md` for architecture
and the data model.

All commands below assume you're in this repo's directory with the stack
already built (`docker compose build`), and use `docker compose exec` --
the API has no host port published on purpose (see `docker-compose.yml`),
so commands run *inside* the container rather than against `localhost` from
the host.

## Joining dns-switcher-net from a sibling project

When another project (e.g. AloBot) needs to reach this service by name, it
joins `dns-switcher-net` as an external network. **Do not** also put that
project's own services with generic names (`db`, `redis`, `app`, ...) on
that same network, and don't add anything of this project's own beyond the
`dns-switcher` app service to it either — this project's own Postgres is
deliberately isolated on the private `dns-switcher-internal` network
instead (see `docker-compose.yml`).

This is a real incident, not a hypothetical: the first attempt at this
integration put AloBot's `bot` service on `dns-switcher-net` while its own
Postgres service was *also* named `db` — the same service name
`dns-switcher`'s own Postgres used at the time. Once joined, Docker's DNS
resolution for the bare hostname `db` from AloBot's `bot` container became
ambiguous across the two networks, and its connections silently resolved
to *this* project's Postgres instead of its own, crash-looping the live
production bot with a misleading `password authentication failed` error
(looks like a credentials bug; is actually a DNS collision). Fixed by
moving this project's Postgres off the shared network entirely, so no
sibling project's service names can ever collide with it again, regardless
of what either project calls its own database service.

## First-time setup

1. Copy `.env.example` to `.env` and fill in `CLOUDFLARE_API_TOKEN` and a
   generated `INTERNAL_API_SHARED_SECRET` (`openssl rand -hex 32`).

2. Bring the stack up and apply migrations:

   ```
   docker compose up -d
   docker compose exec dns-switcher alembic upgrade head
   ```

3. Seed your real domains and datacenters. Copy the example file (kept out
   of git) and fill in real values -- Cloudflare zone IDs (Cloudflare
   dashboard > your domain > Overview, right sidebar) and each
   datacenter's IP:

   ```
   cp scripts/seed.example.yaml scripts/seed.local.yaml
   # edit scripts/seed.local.yaml
   docker compose exec dns-switcher python -m scripts.seed --file scripts/seed.local.yaml
   ```

   You'll also need `DnsTarget` rows for every subdomain you want to
   manage -- there's no bulk-import script for these yet, only the
   seed script's domains/datacenters coverage. Insert them directly via
   `docker compose exec postgres psql -U dns_switcher -d dns_switcher`, e.g.:

   ```sql
   INSERT INTO dns_targets (domain_id, name, record_type, proxied)
   VALUES (1, 'www', 'A', false);
   ```

   Leave `cloudflare_record_id` and `current_datacenter_id` unset -- the
   next step fills the former in, and the sync step's mismatch report
   tells you the correct value to set for the latter.

4. Run the initial Cloudflare sync. This matches each `DnsTarget` to its
   real Cloudflare record by name+type, fills in `cloudflare_record_id`,
   and reports (without changing) any target whose `current_datacenter_id`
   doesn't match what Cloudflare's record content actually resolves to --
   expected for every target on a first run, since none has a
   `current_datacenter_id` set yet:

   ```
   docker compose exec dns-switcher python -m scripts.sync
   ```

   Read its "datacenter mismatches" output and set each target's
   `current_datacenter_id` accordingly via psql (matching the resolved
   datacenter it reports) before doing any switch through the API/bot --
   `execute_single_switch`/`execute_bulk_switch` always re-derive their
   diff from `current_datacenter_id`, so a stale or missing value there
   produces a misleading dry-run.

## Rotating the Cloudflare API token

1. In the Cloudflare dashboard, create a new API token with the same
   Zone:DNS:Edit scope on the two managed zones.
2. Update `CLOUDFLARE_API_TOKEN` in `.env`.
3. Recreate the container so it picks up the new value:
   ```
   docker compose up -d dns-switcher
   ```
4. Verify it works: `docker compose exec dns-switcher python -m scripts.sync`
   should complete without a `CloudflareApiError`.
5. Only then revoke the old token in the Cloudflare dashboard.

## Reading the audit log directly from Postgres (bot is down)

```
docker compose exec postgres psql -U dns_switcher -d dns_switcher -c "
SELECT id, created_at, actor, action_type, status, dns_target_id,
       previous_datacenter_id, new_datacenter_id, error_message
FROM audit_log_entries
ORDER BY created_at DESC
LIMIT 20;
"
```

## Rolling back a change manually via the API (Telegram is unreachable)

Find the `audit_log_entry_id` to roll back (see the psql query above, or
`GET /audit-log` the same way below), then call the rollback endpoint from
*inside* the container -- there's no `curl` in this image, so use Python's
`httpx` (already a dependency) instead:

```
docker compose exec dns-switcher python -c "
import httpx
r = httpx.post(
    'http://localhost:8000/rollback/<audit_log_entry_id>',
    json={'actor': 'manual-ops'},
    headers={'X-Internal-Secret': '<your INTERNAL_API_SHARED_SECRET>'},
)
print(r.status_code, r.json())
"
```

Only a `status: success` entry can be rolled back (rollback re-executes the
switch back to `previous_datacenter_id`, so there must be one to go back
to) -- the response's `success` field tells you whether it worked; a
non-2xx status code with a `detail` field means the roll back was rejected
(e.g. already rolled back, or the entry doesn't exist).
