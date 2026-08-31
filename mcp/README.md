# netmon MCP server

A stdio MCP server that exposes the netmon hub's read API as tools, so an agent
can answer "how is the connection doing?" and "is this bad enough to complain to
Cogeco about?" without paging thousands of raw sample rows into its context.

Runs on the Mac; the hub runs in the `netmon-hub` container on the VPS.

## Tools

| Tool | What it does |
| --- | --- |
| `netmon_summary(probe?)` | The `/api/summary` rollup - 24h ping, 7d speed, latest modem channels, 24h HTTP uptime. The fast "how are we doing" call. |
| `netmon_probes()` | Every probe that has ever reported, with last-seen and staleness. |
| `netmon_alerts()` | Alerts currently firing on the hub. |
| `netmon_ping(hours?, probe?, limit?)` | Raw ping samples. |
| `netmon_speed(days?, probe?, limit?)` | Raw speedtest samples. |
| `netmon_modem(hours?, probe?, limit?)` | Raw DOCSIS per-channel samples. |
| `netmon_http(hours?, probe?, limit?)` | Raw HTTP availability checks. |
| **`netmon_incident_report(days=1, probe?)`** | The analysis. See below. |
| `netmon_access_check()` | Diagnoses this server's own path to the hub. Call it when another tool errors, to tell "hub is down" from "this Mac can't reach it". |

The raw-sample tools return the most recent `limit` rows (default 300) and say
how many matched, because a day of ping data is a few thousand rows and dumping
those into a conversation costs a lot of context to say very little.

### `netmon_incident_report`

Pulls every raw sample for the window in **one** round trip and reduces it to
numbers with timestamps attached:

- **packet loss** - share of checks that lost anything, per target; worst
  15-minute windows (same rule the hub's own loss alert uses, so the report and
  the ntfy pushes can't disagree); contiguous loss episodes with durations.
- **latency** - p50/p95/p99/max overall and per target, count over 100 ms,
  the ten worst spikes with timestamps.
- **speed** - min/p05/median/max down and up; shortfall against the provisioned
  plan when it's configured; bufferbloat as loaded-minus-idle latency; the
  slowest tests; tests that returned 0 Mbps without reporting an error.
- **modem/DOCSIS** - uncorrectable and corrected FEC errors *accrued inside the
  window*, channels below the SNR floor, channels outside the signal spec
  window, each with the channel, frequency and timestamps.
- **outages** - runs where every target lost every packet; probe reporting gaps
  (separately, and labelled - see below); HTTP-target downtime per URL.
- **verdict** - `complaint_worthy`, severity counts, and `quotable_facts`:
  sentences with concrete figures and timestamps meant to be read out verbatim.

Three things it gets right that a naive pass over the same rows would not:

1. **FEC counters are cumulative and reset on modem reboot.** The raw
   `uncorrectable` value is a lifetime total, so a big number says nothing about
   the window you asked about. The report differences per channel between
   consecutive polls and treats a decrease as a counter reset, not as negative
   errors. A channel sitting at 5,000,000 that never moves is not a fault; one
   that climbs by 500 in an hour is.
2. **A gap in samples is not an outage.** The probe keeps a local outbox and
   backfills with the original timestamps once it can reach the hub again, so a
   real internet outage appears as samples with 100% loss, *not* as missing
   samples. Missing samples mean the probe host was off or asleep. The two are
   reported separately and the gap section says so explicitly.
3. **Implausible measurements are called out rather than believed.** A median
   round trip of 0.5 ms to 1.1.1.1 is physically impossible over cable -
   something on the path is answering the ICMP locally. The report raises this
   as an informational finding and repeats it under `verdict.data_caveats`, but
   never as a `quotable_fact`, because our own broken instrumentation is not
   evidence against the ISP. (This is live today on the `imac` probe.)

Modem analysis is keyed per probe on purpose: channel ids from two different
modems aren't comparable, so pooling them would invent channels that don't
exist. Loss/latency/speed pool by default; pass `probe=<name>` to scope them,
and the verdict warns when more than one probe contributed.

Thresholds (SNR floor, signal spec window, latency and bufferbloat limits, the
plan) are all configurable in `.env` and echoed back in the report under
`thresholds_used`, so any number in the output can be traced to the rule that
produced it. **`NETMON_PLAN_DOWN_MBPS` / `NETMON_PLAN_UP_MBPS` are deliberately
unset** - a guessed plan figure would end up quoted at Cogeco. Until they're
filled in from the bill, shortfall is measured self-relatively (against the p95
this line achieved in the same window) and clearly labelled as such.

## How it reaches the hub

Default: **ssh to the VPS and curl the hub's loopback port** (`NETMON_ACCESS=ssh`).

The hub is published on `127.0.0.1:8000` on the VPS only. Every endpoint except
`/api/ingest` and `/healthz` requires the `X-Netmon-Proxy-Secret` header, and on
the public hostname the read API additionally sits behind Authentik forward-auth,
which a headless client can't complete. The VPS's sshd has TCP forwarding
disabled, so `ssh -L` tunnels get "administratively prohibited" - but running a
*command* over ssh is not forwarding, and works fine.

Three options were on the table. The other two:

- **A Traefik API-bypass route** for the read-only `/api/*` GETs on the
  tailnet-only entrypoint, mirroring how `/api/ingest` already bypasses
  forward-auth. Cleanest-looking, and rejected. The IaC that owns that config
  (`~/p/vps`, `roles/sso_app`) states the invariant it would break, in the
  config itself: *"The api_bypass_paths routers must not have [the proxy-secret
  middleware]: those exist for clients that authenticate to the app directly,
  and handing them a proxy-trust token would let a request skip forward auth and
  still be treated as vouched for."* And `group_vars/all.yml` records the intent
  for netmon specifically: *"Everything else (the dashboard, the read API) stays
  behind forward-auth."* Either way of wiring it is worse than what we have: put
  the middleware on the bypass route and every tailnet client reads all netmon
  data with no credential at all; leave it off and the shared proxy-trust secret
  has to be distributed to every client that wants to read, which is exactly the
  property that secret exists to avoid. Building a read-only MCP tool is not a
  good reason to reverse a deliberate, documented authorization boundary.
- **Running the MCP server on the VPS** next to the hub. That means HTTP
  transport over the tailnet instead of stdio, which is a network service to
  deploy, authenticate and keep running - a lot of moving parts for a personal
  tool, and it would need its own auth story or it becomes the open read
  endpoint the previous option was rejected for. (The code supports this: set
  `NETMON_ACCESS=http` and it talks to `127.0.0.1:8000` directly with the secret
  from `.env`. That path exists for running on the box, not as the default.)

Why ssh wins on more than just being least invasive:

- **The secret never leaves the VPS.** The remote snippet reads it out of the
  hub's own env file (`/opt/netmon-hub/.env`, mode 0600, owned by `deploy`), so
  this Mac's only credential is its ssh key. Nothing to sync when the secret
  rotates, and nothing to leak from the laptop.
- **Authorization is already the right shape.** Reading the data requires the
  `deploy` ssh key, which is strictly narrower than "any host on the tailnet
  that knows a shared secret".
- **Zero infra change.** No Traefik route, no Ansible run, no new listener, and
  nothing that can take Traefik down for every app on the box. (`group_vars`
  records that netmon's *first* deploy did exactly that for ~3 minutes.)
- **Batching makes it fast.** `netmon_incident_report` needs six endpoints;
  they go over one ssh connection as one remote script, and `ControlMaster`
  keeps the connection warm across calls. Measured: ~900 ms cold, ~115 ms warm
  for a full authenticated round trip. The handshake, not the query, was the
  cost, and it's paid once.

The one real drawback: it depends on the ssh path staying available, so the MCP
server can't read the hub if sshd or the tailnet is down. `netmon_access_check`
exists to make that immediately obvious rather than a confusing tool error.

Paths are validated against a strict allowlist (`^/api/[a-z]+(\?[safe chars])?$`)
before they can reach the remote shell, and every parameter is URL-encoded, so
nothing a caller supplies can break out of the heredoc the remote script reads
its request list from.

## Registered in Hermes

In `~/.hermes/config.yaml` under `mcp_servers`:

```yaml
  netmon:
    command: /Users/sumeet/p/netmon/mcp/.venv/bin/python
    args:
      - /Users/sumeet/p/netmon/mcp/run_server.py
    cwd: /Users/sumeet/p/netmon/mcp
    timeout: 120
    connect_timeout: 30
    enabled: true
```

MCP tools only load at session start, so Hermes needs a restart to pick it up.

Hermes filters the environment it hands a stdio MCP subprocess down to a small
allowlist (`PATH`, `HOME`, `USER`, …) - notably **not** `SSH_AUTH_SOCK`. That's
fine here: the server reads its own config from `mcp/.env` rather than inherited
environment, and the `deploy` key authenticates without an agent. Both were
verified by launching the server over stdio with exactly that stripped
environment.

## Setup

```
cd ~/p/netmon/mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # defaults are already correct for this setup
```

Check it works, without involving Hermes at all:

```
.venv/bin/python run_server.py --call netmon_access_check
.venv/bin/python run_server.py --call netmon_summary probe=imac
.venv/bin/python run_server.py --call netmon_incident_report days=2 probe=imac
.venv/bin/python run_server.py --list
```

Tests - synthetic samples built to trip each detector, since the live
connection is healthy and never exercises the FEC/SNR/loss/outage paths:

```
.venv/bin/python tests/test_analysis.py
```

## Layout

```
run_server.py          entry point; also a --call CLI for testing
netmon_mcp/config.py   all config, from .env with defaults
netmon_mcp/transport.py  the ssh (and http) path to the hub
netmon_mcp/analysis.py   the incident report
netmon_mcp/server.py     MCP tool definitions
tests/test_analysis.py
```

Secrets live only in `.env` (gitignored) and on the VPS. Nothing sensitive is
committed.
