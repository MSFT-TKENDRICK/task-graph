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
uv venv
uv pip install -e ".[dev]"     # add --native-tls on the Microsoft network

tg doctor       # what's wired up, what needs attention
tg sync         # pull from every available source
tg triage       # what to do next, and why
```

Then wire it into Copilot so you can just ask:

```powershell
tg init         # registers the MCP server in ~/.copilot/mcp-config.json
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
servers (`agency mcp ado`, `agency mcp mail`, …), which already hold your
domain-joined Entra session — so this project never handles a credential.
GitHub uses the `gh` CLI you are already signed into.

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
| `tg status` / `tg doctor` / `tg rebuild` / `tg init` | operations |

Every command supports `--json`.

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

266 tests, all offline. Verified end-to-end on Windows on ARM64 against live
GitHub and Agency's ADO and Mail MCP servers.

Not yet implemented: Teams, Calendar and Planner connectors (the interface is
built for them). MSX is read-and-propose only — its milestone state-transition
semantics are undocumented, so that action refuses to execute until verified
against a live tenant.