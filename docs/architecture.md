# Architecture

## The problem

Work arrives from Outlook, Teams, GitHub (issues, PRs, discussions, project
boards), Azure DevOps and MSX. The same deliverable is frequently tracked in two
or three of those at once, each with its own state machine and its own idea of
"done". There is no single place to see what actually needs doing, and closing
work out means visiting each system in turn.

## Shape of the solution

```
 sources ──► connectors ──► ingest ──► [ activegraph Runtime ]
 (mail/ado/                              │  event log = source of truth
  github/…)                              │  (SQLite, append-only)
                                         ▼
                              SqliteGraphStore  (projection, rebuildable)
                              objects · relations · patches
                              + FTS5 + float32 embedding BLOBs
                                         │
                     ┌───────────────────┼───────────────────┐
                     ▼                   ▼                   ▼
                  dedupe             priority           remediation
              (hybrid retrieval)   (weighted score)   (proposals only)
                     └───────────────────┼───────────────────┘
                                         ▼
                            approval queue  ── user ──► corrections
                            (nothing executes without           │
                             an explicit grant)                 │
                                         ▲                      │
                                         └──── learned weights ──┘
```

Two front ends — the `tg` CLI and an MCP server — are thin wrappers over one
service facade (`task_graph.app.TaskGraphApp`), so a capability is written once
and cannot drift between them.

## Three invariants

Everything else is negotiable; these are not.

### 1. The event log is the only source of truth

`~/.task-graph/events.db` is an append-only activegraph event log. Every
ingest, merge, correction, grant and execution is an immutable event.
`~/.task-graph/graph.db` is a **projection** of it — a queryable current-state
view. Losing the projection is recoverable (`tg rebuild` replays the log);
losing the log is not.

This is what makes the schema safe to change aggressively, and it is why the
projection database runs with `synchronous=NORMAL`.

### 2. Nothing mutates a source system without explicit approval

Remediation actions are *proposed*, never taken. `execute_approved` re-reads the
remediation from the store and refuses anything not in state `GRANTED` —
checking a passed-in argument would be bypassable, so it does not. Executing an
already-`EXECUTED` action is refused too, so a retry cannot double-post a
comment. Grants and rejections are themselves events.

The CLI defaults to dry-run everywhere, and the MCP surface deliberately splits
`approve_action` from `execute_action` so no single tool call can both authorise
and perform a change.

### 3. Every judgement is explainable and correctable

No score is a black box. Dedupe and priority both decompose into named features,
and those features are combined using weights that live in
`~/.task-graph/weights.json`. A user correction nudges the weights that were
responsible, so disagreeing with the system actually changes it.

## Layers

### Connectors

Each connector normalises a source system into `SourceItem` — a flat record with
a **stable `source_uri`**, which is the anchor for idempotent ingest.

Most connectors are MCP *clients* that spawn `agency mcp <name>` as a stdio
subprocess. This is the single most useful thing about running inside Microsoft:
Agency's first-party MCP servers (`ado`, `mail`, `teams`, `calendar`, `planner`,
`workiq`, `m365-copilot`, …) already hold ambient, domain-joined Entra auth, so
this project never handles a credential. GitHub is the exception — it uses the
already-authenticated `gh` CLI.

Adding a source means implementing `Connector` and registering it. Nothing
downstream changes.

### Ingest

Deliberately dumb: one `source_item` per source record, one `task` per
`source_item`, joined by `EVIDENCE_OF`. It makes no judgement about whether two
records are the same work — that is dedupe's job, and keeping them apart means a
bad merge can be undone without re-fetching anything.

Re-running a sync updates in place. A content fingerprint over only the
*meaningful* fields prevents a bumped `updated_at` from causing pointless
re-embedding.

Closure across several sources is conservative: once a task is backed by more
than one system, it is only marked done when **every** backing source agrees.
Closing an ADO item must not silently close work still open in MSX.

### Dedupe

Candidates come from hybrid retrieval — FTS5 BM25 plus vector cosine, fused with
**Reciprocal Rank Fusion**. RRF is used rather than a weighted score blend
because BM25 and cosine live on different, corpus-dependent scales; RRF only
looks at rank, so it needs no re-tuning as the graph grows.

Pairs are then scored on named features:

| feature | why it matters |
| --- | --- |
| `cross_reference` | one record names the other's id (`AB#12345`) — near-proof |
| `shared_external_ref` | both reference the same third item |
| `title_similarity` | token overlap blended with character ratio |
| `embedding_similarity` | semantic similarity, for differently-worded work |
| `same_owner`, `temporal_proximity` | weak corroboration |
| `same_source_penalty` | two GitHub issues are usually genuinely different |

The weighted sum is squashed through a logistic curve rather than divided by the
sum of weights — dividing would assume every feature fires at once, which never
happens, and would push even decisive evidence into the middle of the range.

Only an explicit cross-reference clears the auto-link threshold. Everything else
is raised as an activegraph **patch** awaiting approval, which means rejections
carry a reason and are retained automatically for the learner.

### Priority

Six independently testable factors, each normalised to 0–1: `urgency`,
`source_importance`, `blocking` (graph centrality over `BLOCKS`/`DEPENDS_ON`,
depth-bounded and cycle-safe), `staleness`, `explicit_ask`, `owner_is_me`. The
result carries both the per-factor breakdown and a prose explanation, because a
ranking the user cannot interrogate is a ranking they will not trust.

### Remediation and approval

Typed actions with a `render_preview` that produces the exact mutation
("Set ADO #12345 state: Active → Resolved") and an executor that is unreachable
without a grant. Actions carry a `verified` flag; MSX milestone transitions are
registered but `verified=False` and refuse to execute, because their write
semantics could not be confirmed against a live tenant.

### Learning

A correction is stored as a first-class `correction` object, then folded into
the weights. Blame is assigned in proportion to how loudly each feature argued
for the rejected conclusion, so a feature that barely contributed is barely
moved. Learning is idempotent — corrections are marked applied.

`Corrector.simulate(candidate_weights)` replays past merge decisions under
different weights and reports which would change, answering "would learning from
this have helped?" against real history rather than intuition.

## Storage

`SqliteGraphStore` implements activegraph's `GraphStore` ABC. Structural query
hooks (`find_objects`, `find_objects_in_types`, `find_relations`) are pushed down
into indexed SQL. `neighborhood` runs the base class's exact algorithm but
fetches only edges incident to the current frontier instead of scanning all of
them — keeping the algorithm identical is what guarantees the results match.

`match_chain` is deliberately *not* overridden: it is defined in terms of the
pushed-down hooks, so it gets the speedup for free without risking divergence
from activegraph's homomorphic matching rules.

### Why not sqlite-vec

The obvious choice for vectors-in-SQLite is `sqlite-vec`. It publishes no
`win_arm64` wheel for any version, and this runs on Windows on ARM with an ARM64
Python, which cannot load an amd64 extension DLL. Building C from source was not
an acceptable install step.

It also is not needed. A personal task graph is O(10k) nodes; 10k × 512 float32
is 20 MB, and one brute-force `matrix @ vector` is well under 100 ms — faster
than an ANN index once its own overhead is counted, and exact rather than
approximate. Vectors are stored L2-normalised so cosine reduces to a dot
product. `VectorIndex` is a protocol, so an ANN backend can drop in unchanged if
the graph ever outgrows this.

`cryptography` has the same gap, which is why `pyjwt`'s `crypto` extra is
overridden out in `pyproject.toml` and the MCP server is stdio-only.

## Data layout

| path | contents | disposable |
| --- | --- | --- |
| `~/.task-graph/events.db` | append-only event log | **no** |
| `~/.task-graph/graph.db` | objects, relations, patches, FTS5, embeddings | yes |
| `~/.task-graph/weights.json` | learned dedupe and priority weights | yes (resets to defaults) |

Nothing is stored in the repository, and no credential is stored anywhere: all
access is ambient.
