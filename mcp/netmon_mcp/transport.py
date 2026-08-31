"""How this MCP server reaches the netmon hub API.

Two modes, selected by NETMON_ACCESS:

**ssh** (default) - run `curl` against the hub's loopback port on the VPS over
an ssh session and read the JSON back on stdout. Chosen over adding a Traefik
API-bypass route; the reasoning is in README.md. Two properties worth noting:

  1. The proxy secret never leaves the VPS. The remote snippet reads it out of
     the hub's own env file (`/opt/netmon-hub/.env`, mode 0600, owned by the
     deploy user), so this Mac's only credential is its ssh key. A Traefik
     bypass route would instead require handing the shared proxy-trust secret
     to every client that wants to read.
  2. Requests are batched: one ssh round trip fetches every endpoint an
     incident report needs, instead of four. That matters because the ssh
     handshake (~0.4s) dominates the actual query time.

The VPS's sshd has TCP forwarding disabled, so `ssh -L` tunnels are not an
option - but running a command is, which is all this needs.

**http** - plain HTTP with the proxy-secret header supplied from config. For
running this server on the VPS itself, or through a tunnel if one ever exists.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import config

TAB = "\t"

# Paths are built by this module's callers from validated parameters, never
# from free-form model input. This is the belt-and-braces check: whatever ends
# up in the remote heredoc must not be able to contain a quote, a newline, a
# space or a shell metacharacter.
_PATH_RE = re.compile(r"^/api/[a-z]+(?:\?[A-Za-z0-9_=&.%+\-]*)?$")


class HubError(RuntimeError):
    """The hub (or the path to it) failed to produce usable data."""


def _check_path(path: str) -> str:
    if not _PATH_RE.match(path):
        raise HubError(f"refusing to request unsafe path: {path!r}")
    return path


# Read the secret and header name out of the hub's env file, fetch each
# requested path, and emit one "key<TAB>status<TAB>body" line per request.
# FastAPI's JSON responses contain no literal newlines (newlines inside strings
# are escaped as \n), so one line per response is unambiguous.
_REMOTE_SCRIPT = """set -eu
env_file={env_file}
base={base}
tmo={tmo}
hdr=$(sed -n 's/^[ \t]*NETMON_PROXY_SECRET_HEADER=//p' "$env_file" | head -n1 | tr -d '\\r')
sec=$(sed -n 's/^[ \t]*NETMON_PROXY_SECRET=//p' "$env_file" | head -n1 | tr -d '\\r')
[ -n "$hdr" ] || hdr=X-Netmon-Proxy-Secret
[ -n "$sec" ] || {{ echo "netmon-mcp: no NETMON_PROXY_SECRET in $env_file" >&2; exit 3; }}
while IFS='\t' read -r key path; do
  [ -n "$key" ] || continue
  out=$(curl -sS -m "$tmo" -H "$hdr: $sec" -w '\\n%{{http_code}}' "$base$path" 2>&1) || out='
000'
  code=$(printf '%s' "$out" | tail -n1)
  body=$(printf '%s' "$out" | sed '$d' | tr -d '\\n')
  printf '%s\t%s\t%s\\n' "$key" "$code" "$body"
done <<'NETMON_REQS'
{requests}
NETMON_REQS
"""


def _ssh_argv() -> list[str]:
    # ControlMaster keeps the TCP+auth handshake warm between calls, so a burst
    # of tool calls in one conversation pays for the handshake once. %C is a
    # short hash of the connection, which keeps the socket path under the
    # ~104-char sun_path limit on macOS.
    control_path = Path.home() / ".ssh" / "netmon-mcp-cm-%C"
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "LogLevel=ERROR",
        "-o", f"ConnectTimeout={config.SSH_CONNECT_TIMEOUT}",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={control_path}",
        "-o", "ControlPersist=120",
        config.SSH_TARGET,
    ]


def _fetch_many_ssh(paths: dict[str, str]) -> dict[str, Any]:
    request_lines = "\n".join(f"{key}{TAB}{_check_path(p)}" for key, p in paths.items())
    script = _REMOTE_SCRIPT.format(
        env_file=shlex.quote(config.SSH_ENV_FILE),
        base=shlex.quote(config.SSH_HUB_URL),
        tmo=shlex.quote(str(config.REMOTE_CURL_TIMEOUT)),
        requests=request_lines,
    )
    argv = _ssh_argv() + ["sh", "-c", shlex.quote(script)]

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=config.SSH_TIMEOUT,
        )
    except FileNotFoundError as exc:  # no ssh on PATH
        raise HubError(f"ssh not available: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HubError(
            f"ssh to {config.SSH_TARGET} timed out after {config.SSH_TIMEOUT}s"
        ) from exc

    if proc.returncode != 0 and not proc.stdout.strip():
        raise HubError(
            f"ssh to {config.SSH_TARGET} failed (exit {proc.returncode}): "
            f"{(proc.stderr or '').strip()[:500]}"
        )

    # The VPS prints a login banner, but on stderr - stdout is only our lines.
    # Still, skip anything that isn't shaped like one, rather than trusting that.
    out: dict[str, Any] = {}
    for line in proc.stdout.splitlines():
        parts = line.split(TAB, 2)
        if len(parts) != 3 or parts[0] not in paths:
            continue
        key, code, body = parts
        out[key] = _decode(key, paths[key], code, body)

    missing = [k for k in paths if k not in out]
    if missing:
        raise HubError(
            f"no response for {', '.join(missing)} via ssh {config.SSH_TARGET}; "
            f"stderr: {(proc.stderr or '').strip()[:300]}"
        )
    return out


def _decode(key: str, path: str, code: str, body: str) -> Any:
    if code == "000":
        raise HubError(f"{key} ({path}): curl could not reach the hub: {body[:300]}")
    if code != "200":
        raise HubError(f"{key} ({path}): hub returned HTTP {code}: {body[:300]}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HubError(f"{key} ({path}): hub returned non-JSON: {body[:300]}") from exc


def _fetch_many_http(paths: dict[str, str]) -> dict[str, Any]:
    if not config.PROXY_SECRET:
        raise HubError(
            "NETMON_ACCESS=http needs NETMON_PROXY_SECRET set (every endpoint "
            "except /api/ingest and /healthz requires the proxy-secret header)"
        )
    out: dict[str, Any] = {}
    for key, path in paths.items():
        url = config.HUB_URL + _check_path(path)
        req = urllib.request.Request(
            url, headers={config.PROXY_SECRET_HEADER: config.PROXY_SECRET}
        )
        try:
            with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "replace")
                out[key] = _decode(key, path, str(resp.status), body)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise HubError(f"{key} ({path}): hub returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise HubError(f"{key} ({path}): cannot reach {config.HUB_URL}: {exc.reason}") from exc
    return out


def fetch_many(paths: dict[str, str]) -> dict[str, Any]:
    """Fetch several hub endpoints at once, keyed by a caller-chosen name."""
    if not paths:
        return {}
    if config.ACCESS_MODE == "http":
        return _fetch_many_http(paths)
    if config.ACCESS_MODE == "ssh":
        return _fetch_many_ssh(paths)
    raise HubError(f"unknown NETMON_ACCESS={config.ACCESS_MODE!r} (expected 'ssh' or 'http')")


def fetch(path: str) -> Any:
    """Fetch a single hub endpoint."""
    return fetch_many({"r": path})["r"]


def describe_access() -> dict[str, Any]:
    """How this server is currently configured to reach the hub (no secrets)."""
    if config.ACCESS_MODE == "http":
        return {
            "mode": "http",
            "hub_url": config.HUB_URL,
            "proxy_secret_header": config.PROXY_SECRET_HEADER,
            "proxy_secret_configured": bool(config.PROXY_SECRET),
        }
    return {
        "mode": "ssh",
        "ssh_target": config.SSH_TARGET,
        "hub_url_on_vps": config.SSH_HUB_URL,
        "secret_read_from": f"{config.SSH_ENV_FILE} (on the VPS, never copied here)",
    }
