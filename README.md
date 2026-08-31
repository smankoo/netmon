# netmon

Self-hosted internet + IPTV quality monitor, split into two independent
pieces:

- **`probe/`** — runs on a machine behind the connection you're measuring (a
  laptop, an always-on desktop, a Raspberry Pi). Does the actual measurements
  and pushes results out. Holds nothing long-term and serves no UI - if it
  can't reach the hub, it just queues data locally and keeps retrying.
- **`hub/`** — runs centrally (a VPS, a NAS, anywhere the probes can reach).
  Receives data from one or more probes, stores it, and serves the dashboard.

There's also **`mcp/`** — an optional stdio MCP server that exposes the hub's
read API to an agent, including an `netmon_incident_report` tool that reduces the
raw samples to the loss/latency/DOCSIS/outage figures an ISP complaint actually
needs. It's not part of the measurement pipeline; see `mcp/README.md`.

They only talk to each other over one HTTP endpoint (`POST /api/ingest`),
authenticated with a per-probe bearer token. The hub never touches your
modem or home network; the probe never serves a UI or holds history.

## What the probe collects

- **Ping** (every `PING_INTERVAL_SEC`, default 60s) — latency and packet
  loss to a couple of stable targets.
- **Speed + bufferbloat** (every `SPEEDTEST_INTERVAL_SEC`, default 30 min) —
  download/upload throughput plus idle-vs-loaded latency during the test.
- **Modem DOCSIS stats** (every `MODEM_POLL_INTERVAL_SEC`, default 15 min) —
  per-channel signal/SNR and corrected/uncorrectable FEC error counts from
  the Hitron modem admin UI (driven via a headless browser - the modem's
  raw HTTP API rejects scripted logins).
- **HTTP availability** (every `HTTP_CHECK_INTERVAL_SEC`, default 60s) —
  reachability/latency of arbitrary URLs, e.g. an IPTV portal. This only
  tells you if the portal/middleware itself is up - not whether an
  authenticated video stream is stalling, since that would need credentials
  the probe intentionally doesn't hold.

## Running the hub

```
cd hub
cp .env.example .env
# generate a token per probe: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# edit .env: HUB_PROBE_TOKENS=macbook:<that token>
docker compose up -d --build
```

Dashboard at `http://<hub-host>:8000`. Put it behind a reverse proxy with
TLS if it's exposed to the internet - it's currently plain HTTP internally.

## Running a probe (on the machine actually behind your home connection)

```
cd probe
cp .env.example .env
# edit .env: HUB_URL, HUB_TOKEN (same token you generated above),
# MODEM_PASS, HTTP_CHECK_URLS, etc.
docker compose up -d --build
```

No ports need to be exposed - the probe only makes outbound connections (to
the modem on the LAN, to speedtest/ping targets, and to the hub).

## Moving a probe to another machine

It's just a container + `data/` (the local outbox, which should normally be
near-empty once caught up):

```
rsync -a probe/data/ newhost:~/netmon-probe/data/
# copy .env, docker-compose.yml, Dockerfile, probe/ too
docker compose up -d --build
```

Works on Raspberry Pi (64-bit OS) as-is - the base image and Playwright's
Chromium both support linux/arm64.

## Adding a second probe

1. Add another `name:token` pair to the hub's `HUB_PROBE_TOKENS`, restart
   the hub.
2. Deploy the probe elsewhere with that token in its `.env`.
3. The hub's API accepts an optional `?probe=<name>` filter
   (`/api/ping`, `/api/summary`, etc.) and `/api/probes` lists everyone
   who's ever reported in with a last-seen timestamp. The dashboard has a
   probe selector; "All probes" averages the summary tiles across them and
   draws one series per probe on the charts (the modem chart always pins to
   a single probe, since channel numbers from different modems aren't
   comparable).

The probe's name comes from the token it authenticates with, never from
anything in the request body - so one probe can't file data under another
probe's name.

## Alerting

If `NTFY_URL` is set (e.g. `https://ntfy.sh/<some-random-topic>`), the hub
pushes notifications when something is actually wrong. ntfy is pub-sub with
no accounts: whoever knows the topic name can read *and* publish to it, so
treat the topic like a password and pick a random one.

What triggers a push:

| Condition | Threshold |
| --- | --- |
| Packet loss | ≥ `LOSS_ALERT_PCT` (10%) over the trailing 15 min, min 3 samples |
| Modem FEC | any uncorrectable errors on a downstream channel |
| HTTP target | 3 consecutive failed checks |
| Probe silent | nothing received for `PROBE_STALE_MINUTES` (10) |

Each condition fires once when it goes bad, reminds every
`ALERT_COOLDOWN_SEC` (1h) while it stays bad, and sends one recovery push
when it clears. Active alerts are also shown as a banner on the dashboard
and served from `/api/alerts`.

## Data retention

Raw samples older than `RETENTION_DAYS` (default 180, ~6 months) are pruned
once every `RETENTION_INTERVAL_SEC`. This is a plain delete, not a
downsample-and-keep-forever rollup: the whole point of this tool is spotting
short bursts of loss, and a rollup would average exactly those away. Six
months of raw samples is a few hundred MB at most.

## Notes / gotchas

- `MODEM_USER`/`MODEM_PASS`/`HUB_TOKEN` are read from environment only -
  never hardcoded. `.env` files are gitignored.
- The bufferbloat measurement briefly saturates the link - don't set
  `SPEEDTEST_INTERVAL_SEC` too low if others are using the connection.
- If the modem's LAN IP isn't `192.168.0.1`, set `MODEM_HOST` accordingly.
- Give it at least a few days of data before drawing conclusions - the
  point is to catch the intermittent pattern, not a single snapshot.
- The probe's local outbox (`probe/data/outbox.db`) is exactly what protects
  you from losing data during a real outage - if the hub is unreachable
  because your internet is down, that's precisely the moment you don't want
  to lose the reading. It just backs up locally and drains once connectivity
  returns.
