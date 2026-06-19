# dispatch-mcp

[![CI](https://github.com/selamy-labs/dispatch-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/selamy-labs/dispatch-mcp/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)

`dispatch-mcp` is a small, **security-constrained** [Model Context
Protocol](https://modelcontextprotocol.io) server that exposes exactly one
capability as typed tools: *dispatch a unit of work to an allowlisted
org-user/repo lane*. It turns the "dispatch a task" call that previously lived
as ad-hoc shell out of a skill into constrained, audited tools.

The dispatch **methodology** — which tier to use, how to brief a lane, how to
verify the artifact — stays in the `dispatch-lane` skill. This server is only
the *call*, deliberately narrow so it can be exposed safely.

## Tools

| Tool | Purpose |
| --- | --- |
| `dispatch_unit(orguser, repo, task_description)` | Dispatch a unit of work to an allowlisted lane; returns a handle. |
| `dispatch_status(handle)` | Structured status of a dispatched handle. |
| `dispatch_result(handle)` | Structured result (exit code + detail) for a handle. |
| `dispatch_list()` | Every dispatch launched this session. |

## Security model

This server is built so that exposing it does **not** expose arbitrary command
execution. The properties below are enforced in code and covered by tests.

- **No arbitrary exec.** There is no `run_shell` / `exec` tool. The only thing a
  caller can do is dispatch a unit of work; the command that runs is **fixed by
  the server**, never chosen by the caller.
- **Allowlist, not free-form targets.** `orguser` and `repo` must resolve
  against a config built from the `.repos.json` shape (`orgs` + `repos`). An
  unknown repo, an unknown org-user, or a repo not owned by the named org-user
  is **rejected before anything runs**. There is no wildcard.
- **No shell injection.** The `task_description` is passed to the dispatch
  command as a single argument-vector element (`shell=False`); it is data, never
  interpolated into a shell string. Shell metacharacters in it are inert.
- **No embedded credentials.** Nothing in this package stores a token or key.
  The org-user's identity (SSH key / `GH_TOKEN`) is resolved by the runtime when
  the dispatch process runs as that user.
- **Audit everything.** Every dispatch appends a structured record
  (`who` / `what` / `when` / `handle` / `argv` / `outcome`) to an audit sink.

### Deliberate omissions

If a safe design was not feasible for some richer capability, the safe subset
ships and the capability is omitted rather than adding an unsafe escape hatch:

- No tool lets the caller supply or override the executed command.
- No tool returns or accepts credentials.
- Live status is reported from the server's in-session record; the durable
  record of a running lane lives in that lane's own state files (read those for
  authoritative long-lived status), not via a shell passthrough here.

## Configuration (environment, resolved at call time)

| Variable | Effect |
| --- | --- |
| `DISPATCH_REPOS_JSON` | Path to the allowlist (`.repos.json` shape). Default `~/.repos.json`. |
| `DISPATCH_SSH_HOST` | Host the fixed dispatch template targets. Default `localhost`. |
| `DISPATCH_MAX_ITERATIONS` | Max iterations passed to the dispatch lane. Default `50`. |
| `DISPATCH_TIMEOUT` | Seconds to wait for the dispatch command to return. Default `60`. |

No credentials are read from the environment by this server; identity is the
runtime user's.

## Install

Run directly from GitHub with the MCP extra:

```bash
uvx --from "git+https://github.com/selamy-labs/dispatch-mcp@v0.1.0#egg=dispatch-mcp[mcp]" dispatch-mcp
```

Or with pipx:

```bash
pipx install "dispatch-mcp[mcp] @ git+https://github.com/selamy-labs/dispatch-mcp@v0.1.0"
```

## MCP client config

```json
{
  "mcpServers": {
    "dispatch": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/selamy-labs/dispatch-mcp@v0.1.0#egg=dispatch-mcp[mcp]",
        "dispatch-mcp"
      ],
      "env": {
        "DISPATCH_REPOS_JSON": "/home/you/.repos.json",
        "DISPATCH_SSH_HOST": "localhost"
      }
    }
  }
}
```

## Observability (OpenTelemetry)

The server runs unmodified under
[OpenTelemetry zero-code auto-instrumentation](https://opentelemetry.io/docs/zero-code/python/).
Install the `otel` extra and launch via `opentelemetry-instrument`:

```bash
pipx install "dispatch-mcp[mcp,otel] @ git+https://github.com/selamy-labs/dispatch-mcp@v0.1.0"
OTEL_SERVICE_NAME=dispatch-mcp \
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
OTEL_TRACES_EXPORTER=otlp \
  opentelemetry-instrument dispatch-mcp
```

Config is **vendor-neutral** — point `OTEL_EXPORTER_OTLP_ENDPOINT` at any OTLP
collector; the collector (not this server) owns any Cloud Trace / vendor coupling.

> **stdio safety (required):** this server speaks MCP over stdin/stdout, so its
> stdout carries the JSON-RPC protocol. Export traces/logs via **OTLP only** —
> **never** set `OTEL_TRACES_EXPORTER=console` (or any stdout exporter), which
> would interleave span output into the protocol stream and break the client.

## Architecture

The dispatch logic lives once in `dispatch_mcp.core.Dispatcher`; the MCP server
in `dispatch_mcp.mcp_server` is a thin wrapper that serialises structured
results to JSON and maps expected failures to `ToolError`. All process execution
goes through an **injected transport** (`dispatch_mcp.transport`) that runs a
fixed argument vector with `shell=False`, and all timing through an injected
clock, so the full validate / template / audit path is exercised offline in
tests with a fake transport — no ssh, no tmux, no network. The default
`SubprocessTransport` uses only the standard library, so the core package has
zero runtime dependencies; the `mcp` SDK is an optional extra needed only to run
the server.

## Development

```bash
python -m pip install -e ".[test]"
ruff format --check .
ruff check .
coverage run -m pytest
coverage report --fail-under=95
```

## License

MIT — see [LICENSE](LICENSE).
