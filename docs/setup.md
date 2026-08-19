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
.\run.ps1 setup
```

That creates `.venv`, installs task-graph editable with its dev dependencies,
and is idempotent — re-run it any time. It is also exactly what the GitHub
Copilot app runs as its Setup script, so there is one provisioning path rather
than two that drift apart.

**On the Microsoft corporate network** `files.pythonhosted.org` refuses the TLS
handshake outright, so the default PyPI index cannot serve wheels at all and a
bare `uv pip install` dies on the first package. pip already knows the internal
mirror — it is configured machine-wide in `C:\ProgramData\pip\pip.ini` — so
setup asks pip for its own effective index and hands that to uv, rather than
hard-coding a Microsoft-internal URL into the repository. Off the corporate
network pip reports nothing, uv uses PyPI, and the same code path is correct.

uv is used when it is installed (about 40 seconds here) and pip is the fallback
(about two minutes). To do it by hand:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"    # pip reads pip.ini itself
```

Then check everything:

```powershell
.\run.ps1 doctor
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

Examples below are written as `tg`, which is the console script inside `.venv`.
Either activate the virtualenv (`.venv\Scripts\Activate.ps1`) or put `.\run.ps1`
in front — unknown tasks are forwarded to `tg` unchanged, so `.\run.ps1 init
--dry-run` and `tg init --dry-run` are the same command.

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
| `TASK_GRAPH_PLANNER_PLANS` | comma-separated Planner plan ids |

Sources that need pinning tell you so, and list your real options in the error
rather than making you go and find them.

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
.\run.ps1 sync
.\run.ps1 shell                 # then type the commands below without the `tg`
```

```
tg> triage                      # what to do next, and why
tg> why <task-id>               # the full factor breakdown
tg> merges                      # duplicates awaiting your call
tg> approve merge <patch-id>
tg> reject merge <patch-id> --reason "different releases"
tg> correct <task-id> --not-a-task --reason "newsletter"
tg> learn                       # fold those corrections into the weights
tg> exit
```

The shell is the same CLI dispatched inside one process, so only the first
command pays the import cost. A leading `tg` is accepted and ignored, `help`
lists the commands, group options work (`--json triage`), and a command that
fails prints its error and leaves you at the prompt. Anything you can type
there also works as `tg <command>` or `.\run.ps1 <command>`, and
`tg shell -c triage -c merges` runs a fixed sequence without prompting.

Corrections only change behaviour after `learn`, which reports exactly which
weights moved.

## Background jobs

`sync`, `doctor`, `rebuild` and `learn` run as background jobs, because all of
them block on `agency mcp` servers. In the shell they start and follow:

```
tg> sync
[job 3] sync - Ctrl-C cancels; add & to keep the prompt
| sources 2/6 mail: 12 items (7s)
```

| you type | what happens |
| --- | --- |
| `sync` | starts job, follows it live, Ctrl-C cancels it |
| `sync &` | starts it and hands the prompt straight back |
| `jobs` | what is running, and how far in |
| `jobs --all` | finished ones too |
| `logs 3` / `logs 3 -f` | that job's captured output |
| `cancel 3` | stop it, and everything it started |

Outside the shell the same jobs are reachable: `tg bg sync --propose` starts
one, `tg jobs` lists them, `tg logs 3` reads one. Job state lives in
`~/.task-graph/jobs/`, so a job outlives the shell that started it and the MCP
server sees the same list.

Cancelling kills the process tree, not just the job process. That matters:
the cost of a sync is the `agency mcp` servers it spawned, and stopping only
the parent would leave them running with their sessions open.

`triage` and every other read stays instant while a job runs — both databases
are in WAL mode, so a writer never blocks a reader.

Run one inline instead, with a progress line and Ctrl-C to abort:

```powershell
tg sync            # foreground; the shell is where jobs happen
```

## Running from the GitHub Copilot app

`.github/github-app.yml` wires two scripts:

| script | command | when |
| --- | --- | --- |
| Setup | `python scripts/app_setup.py` | on session create |
| Run | `python scripts/app_run.py` | the Run button |

Both are stdlib-only and import nothing from `task_graph`, because they run
before the virtualenv exists and from whichever shell the app chooses. Run
provisions the environment first, so pressing Run on a session whose setup was
skipped repairs it rather than failing.

The app asks you to review and accept the configuration the first time it sees
it, and again after any edit — including whitespace. Until you accept it, the
app keeps using whatever was configured in the UI.

Run opens the interactive shell when it has a terminal on both ends, which the
app's Terminal panel gives it. The script-runner pane pipes stdout, so a prompt
there would wait for a typist who cannot reach it; there it prints `doctor` and
`triage` instead and names the command that gets you the real shell. Override
the detection with `TASK_GRAPH_RUN_INTERACTIVE=1` or `=0`.

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
