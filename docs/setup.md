# Setup

## Requirements

- **Python 3.11+**. Verified on 3.13 on Windows on ARM64.
- **[`gh`](https://cli.github.com/)**, already authenticated (`gh auth status`).
- **Microsoft Agency CLI**, for ambient-auth access to ADO, Mail, Teams,
  Calendar and Planner. Without it those connectors report unavailable and
  everything else still works.
- Optional: **Foundry Local** for on-device embeddings.

## Install

```powershell
uv venv
uv pip install -e ".[dev]"
```

**On the Microsoft corporate network** package downloads are routed through an
internal proxy and `files.pythonhosted.org` is blocked. `pip` picks this up from
`C:\ProgramData\pip\pip.ini` automatically; `uv` does not, so pass the index
explicitly and use the platform certificate store:

```powershell
$env:UV_INDEX_URL = 'https://packagefeedproxy.microsoft.io/pypi/simple/'
uv pip install --native-tls -e ".[dev]"
```

Then check everything:

```powershell
tg doctor
```

```
[OK] connector:github: GitHub CLI is authenticated.
[OK] connector:ado: Found 35 tools on agency mcp ado.
[OK] connector:mail: Found 22 tools on agency mcp mail.
[OK] agency: Agency CLI provides ambient-auth MCP servers (mail, ado, teams...)
[OK] embeddings: Hashing fallback (512 dimensions, degraded)
[OK] graph_db_integrity: ok
```

`tg doctor` exits non-zero only on critical failures; a degraded embedder or a
missing optional connector is reported but tolerated.

## Register the MCP server

This is the main way to use the tool — it lets you drive everything
conversationally from Copilot CLI or Agency CLI.

```powershell
tg init                  # merges into ~/.copilot/mcp-config.json
tg init --print-config   # show the snippet without writing
tg init --dry-run        # show what would change
```

`tg init` merges into any existing config, backs it up first, and is idempotent —
it will not disturb other MCP servers you have registered.

## Azure DevOps configuration

`wit_work_item` needs an organisation and a project, and large tenants expose
hundreds of projects. Rather than fan a sync out across arbitrary ones,
task-graph asks you to pin them once. Run a sync and it will list your real
options:

```
ado: Set TASK_GRAPH_ADO_ORG to choose an Azure DevOps organization.
     Available: mseng, 1es, msft-skilling, msazure, contosohotelsdev
```

```powershell
$env:TASK_GRAPH_ADO_ORG = 'mseng'
$env:TASK_GRAPH_ADO_PROJECTS = 'Domino,Falcon'
```

Set these permanently with `setx` so scheduled syncs pick them up.

## On-device embeddings (optional)

Without Foundry Local, task-graph uses a deterministic hashing embedder. It
works and keeps everything reproducible, but semantic matching is much weaker —
so cross-system duplicates that share no wording are more likely to be missed.

```powershell
winget install Microsoft.FoundryLocal
foundry model download qwen3-embedding-0.6b
foundry model run qwen3-embedding-0.6b
```

task-graph auto-detects it. `tg doctor` will stop saying "degraded". Foundry
Local accelerates on Qualcomm Snapdragon X / Hexagon NPU via the QNN execution
provider.

## Environment variables

| variable | purpose |
| --- | --- |
| `TASK_GRAPH_HOME` | state directory (default `~/.task-graph`) |
| `TASK_GRAPH_EMBEDDINGS` | `auto` (default), `foundry`, or `hashing` |
| `TASK_GRAPH_FOUNDRY_ENDPOINT` | default `http://localhost:5273/v1` |
| `TASK_GRAPH_FOUNDRY_MODEL` | default `qwen3-embedding-0.6b` |
| `TASK_GRAPH_IDENTITY` | your email/UPN/aliases, for "assigned to me" scoring |
| `TASK_GRAPH_ADO_ORG` | Azure DevOps organisation |
| `TASK_GRAPH_ADO_PROJECTS` | comma-separated ADO projects |

## Scheduled syncs

`tg sync` is safe to run on a timer — it is read-only against every source, and
ingest is idempotent. It deliberately does **not** generate remediation
proposals unless you pass `--propose`, so a background sync never fills your
approval queue.

```powershell
schtasks /create /tn "task-graph sync" /tr "C:\path\to\.venv\Scripts\tg.exe sync" /sc hourly
```

## Daily use

```powershell
tg sync
tg triage                       # what to do next, and why
tg why <task-id>                # the full factor breakdown
tg merges                       # duplicates awaiting your call
tg approve merge <patch-id>
tg reject merge <patch-id> --reason "different releases"
tg correct <task-id> --not-a-task --reason "newsletter"
tg learn                        # fold those corrections into the weights
```

Corrections only change behaviour after `tg learn`, which reports exactly which
weights moved.

## Troubleshooting

**`agency mcp ado is not available`** — check `agency` is on `PATH` and that you
are on VPN/SSE. Run `agency mcp ado` directly to see its own diagnostics.

**Everything ranks the same** — you probably have no due dates, no blocking
relationships and no identity set. Set `TASK_GRAPH_IDENTITY`.

**Semantic search feels weak** — you are on the hashing fallback. Install
Foundry Local.

**The graph looks wrong** — `tg rebuild` reconstructs it from the event log and
re-embeds. It is non-destructive: the old projection is kept as `graph.db.bak`,
and `events.db` is never touched.

**Dependency install fails with a `cryptography` build error** — something has
pulled `mcp>=1.20`, which depends on `pyjwt[crypto]`. `cryptography` publishes no
`win_arm64` wheel, so it tries to build from Rust source. The pin in
`pyproject.toml` (`mcp>=1.2,<1.20`) exists to prevent exactly this; do not
loosen it on Windows on ARM.
