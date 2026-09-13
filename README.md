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

## Splitting collectors across probes

Don't just copy one probe's `.env` to the second host. Two of the four
collectors are not safe to run on two probes at once:

- **modem** - the Hitron admin UI allows a single concurrent admin session.
  Two probes logging in on a 15-minute cycle will evict each other, putting
  auth failures and gaps into *both* probes' DOCSIS history. Run it on
  exactly one probe.
- **speed** - a speedtest saturates the uplink by design. Two of them
  overlapping means each reads roughly half the line, and the loaded-latency
  figures become meaningless. Either run it on one probe, or stagger it (see
  below).

**ping** and **http** are the opposite: cheap, and genuinely worth running
everywhere. Two vantage points are what distinguish "the internet is down"
from "that one host's link is bad".

Set any interval to `0` to disable a collector. Do *not* disable the modem
collector by blanking `MODEM_USER`/`MODEM_PASS` - it reports an error row
every interval instead of going quiet, which pollutes `modem_samples` and
trips the probe-health alerting.

### Staggering, and why offsets are epoch-anchored

`*_PHASE_OFFSET_SEC` shifts a collector's schedule. Runs are scheduled on
absolute epoch boundaries (`t % interval == offset`), not relative to process
start, so two probes half an interval apart stay that way across restarts. A
startup-relative delay would not: whichever probe restarted last would
re-anchor to its own start time and the two could drift into lockstep,
speedtesting on top of each other. The cost of epoch anchoring is that after a
restart a collector waits up to one full interval before its first sample -
for `speed` at 1800s that is a visible gap.

### The deployment here

Two probes on one uplink, deliberately measuring the difference between them:

| Collector | `iris` (Pi 4, wired GbE) | `imac` (2.4 GHz Wi-Fi) |
| --- | --- | --- |
| ping | 60s, offset 0 | 60s, **offset 30** |
| http | 60s, offset 0 | disabled |
| speed | 1800s, offset 0 | 1800s, **offset 900** |
| modem | 900s, offset 0 | disabled |

`iris` runs in Docker. **`imac` does not, and must not** — see below.

#### The iMac probe runs natively, under launchd

Until 2026-09-13 the iMac probe ran as a container under Colima, and every
latency number it ever reported was fiction. Colima's user-mode network stack
answers ICMP *inside the VM* instead of forwarding it, so `ping` succeeded
against a loopback that was never on the network: **0.58 ms to `1.1.1.1`
averaged over 38,958 samples**, from Montreal, where the same target is 19 ms
from iris on the wire and ~30 ms from this Mac natively.

This is the worst failure mode in the whole system, worse than an outage: the
data was plausible-looking, self-consistent, and *good news*. A probe reporting
sub-millisecond latency to Cloudflare does not look broken, it looks like a
fast connection, so nothing raised an alert and nothing looked wrong on the
dashboard for two weeks. Only comparing two probes exposed it. It also silently
corrupted the speed samples, because `speed.collect_once()` calls the same
`ping_once()` helper for its idle and loaded latency figures — so the
bufferbloat measurement was fiction even with the ping collector disabled.

The fabricated rows were deleted (38,958 `ping_samples`, plus the three latency
columns NULLed on 654 `speed_samples`); the pre-purge database is at
`/opt/netmon-hub/backups/netmon-before-imac-purge.db` on the VPS.

**Telling a pre-cutover `imac` row from a post-cutover one: read
`idle_latency_ms`, not the timestamp.** A speedtest row from that probe with
`idle_latency_ms` under about 1 ms was taken under Colima, because no real path
from this Mac to a speedtest server is sub-millisecond — the native floor is
~24 ms. That test is intrinsic to the row, so it keeps working without anyone
remembering when the container stopped, and it survives a clock skew or a
restore from the pre-purge backup. The timestamp boundary was 2026-09-13 ~12:30,
but prefer the marker: rows NULLed by the purge and rows written natively are
both distinguishable from fiction by it, and only one of those two facts is
written down anywhere.

The native deployment is three files on the Mac, outside any repo, because that
box is hand-managed and is not in the homelab ansible inventory:

```
~/netmon-probe/.env                            0600, holds the hub token
~/netmon-probe/run-probe.sh                    sources .env, sets PATH and OUTBOX_PATH
~/Library/LaunchAgents/ai.netmon.probe.plist   KeepAlive, ProcessType Interactive
~/netmon-probe/.venv                           uv venv, Python 3.12 (system python is 3.9)
```

Three things about it that are not obvious:

- **`OUTBOX_PATH` must be set explicitly.** The container mounted `./data` at
  `/data`, and `config.py` still defaults to `/data/outbox.db` — which on the
  host is an unwritable path outside the sandbox, so the probe would either
  fail or start a fresh queue and abandon undelivered samples.
- **`ProcessType` is `Interactive`, deliberately.** A `Background` LaunchAgent
  is subject to App Nap and timer coalescing, which would delay the
  epoch-anchored slots and add the throttling's own latency to every RTT. A
  measurement job must not be scheduled at the OS's convenience.
- **`PATH` must include `/sbin`**, since `collectors/ping.py` shells out to
  `ping`. launchd hands a job almost no environment.

`collectors/ping.py` already handled BSD `ping` (the `-W` flag means seconds on
Linux and milliseconds on macOS, and the summary line differs), so no code
changed for any of this — it was purely a deployment fault.

Verify it after any change to that box, rather than trusting that it runs:

```bash
ssh sumeets-imac 'tail -3 ~/netmon-probe/logs/probe.err'
ssh sumeets-imac 'cd ~/netmon-probe && ./.venv/bin/python -c "
from probe.collectors import ping; print(ping.collect_once())"'
```

An `avg_ms` under 5 ms to `1.1.1.1` means something is answering ICMP locally
again and the series is worthless until it is fixed.

`speed` runs on both on purpose. Both probes sit behind the same modem, so
the *difference* between wired iris and Wi-Fi imac isolates what Wi-Fi quality
is costing - which a single probe can't show. That only works if the two tests
never overlap, hence the 900s offset.

Two artefacts of this layout, so they aren't later misread as ISP faults:

- Each probe's speedtest inflates the *other* probe's ping latency and loss
  for its duration - roughly 3% of ping samples, in two predictable windows
  per hour. Staggering is what separates those windows; it cannot remove them.
- DOCSIS history is per-probe. Moving the modem collector between hosts
  splits the series at the cutover, and `netmon_incident_report` filtered to
  one probe will not see across that boundary.

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
