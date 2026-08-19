# task-graph

One local, event-sourced graph of everything you actually have to do — pulled
from Outlook, Teams, GitHub, Azure DevOps and MSX, with duplicates across those
systems unified, ranked by what matters, and with remediation actions
**proposed rather than taken**.

```
$ tg triage
   #  PRIORITY  STATE     SOURCES       TITLE                                    WHY
   1     0.464  active    github,ado    Nightly invoice job fails on large ac...  overdue 3 days; you own it; ...
   2     0.432  active    mail          Can you review the Q3 pricing deck be...  due in 1 day; you own it; ...
   3     0.228  triage    ado           Upgrade billing service to .NET 9         you own it; undated work ...
```

Row 1 is one task backed by *two* systems: a GitHub issue whose body says
`AB#77321` and ADO work item 77321 were recognised as the same work and merged.

## Why it exists

The same deliverable is routinely tracked in two or three places, each with its
own state machine and its own idea of "done". There is no single place to see
what needs doing, and closing work out means visiting each system in turn.

## Three guarantees

- **Nothing is written to a source system without your explicit approval.**
  Actions are proposed with an exact preview. `approve` and `execute` are
  separate steps, the executor re-reads approval state from the store before
  running, and an already-executed action cannot run twice.
- **The event log is the only source of truth.** The queryable graph is a
  projection; `tg rebuild` reconstructs it from the log.
- **Every judgement is explainable and correctable.** Priorities and merges
  decompose into named factors, and correcting one moves the weight responsible
  — so disagreeing with it actually changes it.

## Quick start

```powershell
.\run.ps1 setup   # creates .venv, installs editable, picks the right package index

.\run.ps1 doctor  # what's wired up, what needs attention
.\run.ps1 sync    # pull from every available source
.\run.ps1 triage  # what to do next, and why
```

`.\run.ps1` on its own lists every task, and anything it doesn't define is
forwarded to `tg` — so `.\run.ps1 why <id>` works. Activate the virtualenv
(`.venv\Scripts\Activate.ps1`) if you'd rather type `tg` directly.

Triage is a conversation with the tool rather than one command, so there is a
shell that keeps the process warm — the imports cost about a second and a half,
and this pays them once instead of once per command:

```powershell
.\run.ps1 shell
```
```
tg> triage
tg> why task-a1b2c3
tg> correct task-a1b2c3 --lower --reason "not this sprint"
tg> exit
```

Then wire it into Copilot so you can just ask:

```powershell
.\run.ps1 init   # registers the MCP server in ~/.copilot/mcp-config.json
```

> *"What should I work on today?"*
> *"Why is that ranked first?"*
> *"Are any of these the same work?"*
> *"Draft the ADO state change — don't apply it yet."*

See [docs/setup.md](docs/setup.md) for the full setup, including Agency MCP
servers and on-device embeddings.

## How it works

```
 sources ──► connectors ──► ingest ──► [ activegraph event log = truth ]
                                              ▼
                                   SqliteGraphStore (projection)
                                   + FTS5 + embedding vectors
                     ┌────────────────────┼────────────────────┐
                     ▼                    ▼                    ▼
                  dedupe              priority            remediation
                     └────────────────────┼────────────────────┘
                                          ▼
                              approval queue ── you ──► corrections
                                          ▲                  │
                                          └── learned weights ┘
```

Connectors are MCP *clients* that spawn Microsoft Agency's first-party MCP
servers (`agency mcp ado`, `agency mcp mail`, `agency mcp teams`, …), which
already hold your domain-joined Entra session — so this project never handles a
credential. GitHub uses the `gh` CLI you are already signed into.

Ingest is strictly read-only, and that is enforced rather than assumed: every
tool name is tokenised and refused if it looks like a mutation. A substring
check is not enough — Planner's `CreateTask` and Calendar's `DeleteEventById`
would sail past one.

Mail, Teams and Calendar each decide what actually counts as work. Being strict
matters: an early version of the Teams heuristic surfaced 202 "tasks" from chat
history, which is worse than having no Teams connector at all. It now returns
one item per unanswered conversation, within a recent window.

Dedupe combines BM25 and vector similarity via reciprocal rank fusion, but
weights *evidence* over similarity: an explicit cross-reference auto-links,
anything weaker is queued for your approval.

[docs/architecture.md](docs/architecture.md) explains the design and the
trade-offs, including why vector search is brute-force numpy rather than
`sqlite-vec` (no `win_arm64` wheel exists).

## CLI

| command | what it does |
| --- | --- |
| `tg sync` | pull from sources, unify, rank |
| `tg triage` | ranked open work with explanations |
| `tg show ID` / `tg why ID` | task detail / priority breakdown |
| `tg search QUERY` | hybrid lexical + semantic search |
| `tg merges` | proposed duplicate merges |
| `tg approve merge ID` / `tg reject merge ID --reason` | decide a merge |
| `tg actions` | proposed remediations, with previews |
| `tg approve action ID [--execute]` | grant; `--execute` is what runs it |
| `tg reject action ID --reason` | decline an action |
| `tg correct ID --not-a-task/--higher/--lower` | teach it |
| `tg learn` | fold corrections into the weights |
| `tg weights` | inspect or set tuning parameters |
| `tg shell` | interactive prompt; one process, no restart cost |
| `tg bg CMD` / `tg jobs` / `tg logs ID` / `tg cancel ID` | background work |
| `tg status` / `tg doctor` / `tg rebuild` / `tg init` | operations |

Every command supports `--json`.

## Nothing blocks

Slow work is a job. `sync`, `doctor`, `rebuild` and `learn` all spend their time
waiting on `agency mcp` servers, so they run as child processes you can watch,
leave, and kill:

```
tg> sync
[job 3] sync - Ctrl-C cancels; add & to keep the prompt
| sources 2/6 mail: 12 items (7s)
```

- **Progress**, per source, because that is where the seconds go.
- **Ctrl-C cancels** — and kills the `agency` subprocesses too, which is the
  only thing that actually stops an in-flight call.
- **`sync &`** hands the prompt straight back. `triage` stays instant while it
  runs; both databases are WAL, so a job writing never blocks a read.
- **`jobs`, `logs <id>`, `cancel <id>`** work from the shell, the one-shot CLI
  and the MCP server, because job state is on disk rather than in memory. A job
  outlives the shell that started it.

Fast commands stay in-process and instant — spawning a process to run a SQLite
read would cost more than the read.

Connector probes now run concurrently, which took `doctor` from 37s to 12s.

## Running it from the GitHub Copilot app

`.github/github-app.yml` configures the project, so **Setup** provisions `.venv`
when a session is created and **Run** opens the `tg` shell. The app asks you to
review and accept the configuration the first time it sees it.

The Run button opens the interactive shell when it has a terminal — the app's
Terminal panel does. The script-runner pane pipes stdout, so nothing typed there
would ever arrive; it prints `doctor` and `triage` instead, and tells you the one
command that gets you the prompt. Force either with `TASK_GRAPH_RUN_INTERACTIVE`.

## MCP tools

`sync_sources`, `list_tasks`, `get_task`, `search_tasks`, `get_task_graph`,
`explain_priority`, `list_pending_merges`, `approve_merge`, `reject_merge`,
`propose_remediations`, `list_pending_approvals`, `preview_action`,
`approve_action`, `execute_action`, `record_correction`,
`learn_from_corrections`, `get_status`, `run_doctor`.

`approve_action` grants permission and explicitly does not execute;
`execute_action` requires a prior grant.

## Data

Everything lives in `~/.task-graph/` (override with `TASK_GRAPH_HOME`):
`events.db` (the log — keep it), `graph.db` (the projection — disposable),
`weights.json` (what it has learned from you). Nothing is stored in the repo,
and no credentials are stored anywhere.

## Status

Verified end-to-end on Windows on ARM64 against **live** GitHub and Agency's
ADO, Mail, Teams, Calendar and Planner MCP servers. All tests run offline.

| source | status |
| --- | --- |
| GitHub — issues, PRs, discussions, project boards | live |
| Azure DevOps — work items | live |
| Outlook mail | live |
| Teams threads | live |
| Calendar | live |
| Planner | live |
| MSX — opportunities, milestones, activities | implemented, **not reachable here** |

MSX needs the `msx-mcp` plugin plus VPN; `agency mcp msx` does not exist on this
machine, so the connector is tested offline against the documented tool surface
and discovers real tool names at runtime. MSX remains read-and-propose only —
its milestone and opportunity state-transition semantics are undocumented, so
those actions refuse to execute until verified against a live tenant.