# Connectors

A connector turns one source system into `SourceItem` records. Everything
downstream — ingest, dedupe, priority, remediation — is source-agnostic, so
adding a system is a self-contained change.

## Shipped connectors

| kind | transport | status |
| --- | --- | --- |
| `github` | `gh` CLI subprocess | live |
| `ado` | `agency mcp ado` | live — 35 tools |
| `mail` | `agency mcp mail` | live — 22 tools |
| `teams` | `agency mcp teams` | live |
| `calendar` | `agency mcp calendar` | live |
| `planner` | `agency mcp planner` | live |
| `msx` | `msx-mcp` plugin | **not reachable on this machine** — see below |

## Why MCP subprocesses

Microsoft's Agency CLI ships first-party MCP servers that already hold the
signed-in user's Entra session. Running `agency mcp <name>` starts one on stdio.
By acting as an MCP *client*, this project inherits that auth and never touches
a credential — no tokens, no refresh flows, no secrets in config.

`AgencyMcpClient` wraps this. It resolves `agency` via `shutil.which`, reports
unavailability instead of crashing when it is missing, presents a synchronous
facade over the async MCP SDK, caches availability probes, bounds them to 6s,
and always terminates the subprocess.

The whole session runs on a single anyio task via a blocking portal. This is not
incidental: the SDK's context managers own anyio cancel scopes, and driving them
from two different `run_until_complete` calls fails teardown with *"attempted to
exit cancel scope in a different task"*.

## Never call a mutating tool

Ingest is read-only. `assert_read_only()` in `mcp_client.py` is the last line of
defence between a scheduled background sync and someone's real board.

It tokenises the tool name — splitting `snake_case`, `camelCase` and
`PascalCase` — and refuses if any token is a mutating verb. **A substring check
is not sufficient, and this was a real bug**: the original guard looked for
`_write`, which silently allowed Planner's `CreateTask` and `UpdateTask` and
Calendar's `DeleteEventById`.

Calendar also shows why the verb list is broader than it first appears:
`AcceptEvent`, `DeclineEvent` and `ForwardEvent` all mutate — replying to an
invite writes to other people's calendars.

The guard errs towards refusing. A false refusal is loud and overridable with
`allow={...}`; a false permit is an unattended sync writing to production.

## Verified tool names

Discovered live via `list_tools()`. **Never invent these** — a connector written
against guessed names once selected `wit_work_item_write` during a read-only
sync.

- **ADO** — reads: `wit_work_item`, `wit_query`, `wit_backlog`, `search_workitem`,
  `core_list_orgs`, `core_list_projects`, `repo_pull_request`.
  Writes to avoid: `wit_work_item_write`, `wit_work_item_comment_write`,
  `wiki_upsert_page`, `repo_create_branch`, `pipelines_write`.
- **Planner** — reads: `QueryPlans`, `GetPlan`, `QueryTasksInPlan`, `GetTask`,
  `GetGoal`, `QueryGoalsInPlan`, `GetUserGroups`.
  Writes: `CreateTask`, `UpdateTask`, `CreatePlan`, `UpdatePlan`, `CreateGoal`,
  `UpdateGoal`.
- **Calendar** — reads: `ListEvents`, `ListCalendarView`, `FindMeetingTimes`,
  `GetRooms`, `GetUserDateAndTimeZoneSettings`.
  Writes: `CreateEvent`, `UpdateEvent`, `DeleteEventById`, `AcceptEvent`,
  `TentativelyAcceptEvent`, `DeclineEvent`, `CancelEvent`, `ForwardEvent`.
- **Teams** — reads: `ListChats`, `ListChatMessages`, `ListTeams`,
  `ListChannels`, `ListChannelMessages`, `SearchTeamsMessages`.

## The contract

```python
class Connector(Protocol):
    kind: SourceKind
    name: str

    def is_available(self) -> ConnectorStatus: ...
    def fetch(self, since: datetime | None = None) -> Iterable[SourceItem]: ...
```

`is_available()` must **never raise**. It returns a `ConnectorStatus` carrying
`available`, a human `detail`, and a `remediation` string telling the user what
to run (`gh auth login`, connect to VPN, …). This is what `tg doctor` renders.

### `source_uri` is the load-bearing field

Ingest is idempotent on `source_uri`, and the projection enforces a unique index
on it. It must be **stable across syncs** and globally unique:

```
github:issue:owner/repo#123      ado:workitem:12345
github:pr:owner/repo#45          planner:task:<id>
github:discussion:owner/repo#7   calendar:event:<id>
mail:<message-id@corp>           teams:message:<chat-id>:<message-id>
msx:opportunity:<id>             msx:milestone:<id>      msx:activity:<id>
```

### `external_refs` drives cross-source dedupe

Populate it with every identifier the record mentions — `AB#12345`, `#123`,
work-item URLs. This is the highest-value field a connector produces: a GitHub
issue whose body says `AB#12345` and ADO work item 12345 auto-link on that
evidence alone, regardless of wording.

### `source_state` must be preserved verbatim

Keep the source system's own state string. Ingest maps it onto our lifecycle via
`STATE_MAP` in `pipeline/ingest.py`, but remediation needs the original
vocabulary to perform a transition ("Active → Resolved").

### Not everything is a task

Mail, Teams and Calendar all need a judgement about what actually constitutes
work. A newsletter, a channel announcement, or a recurring standup the user
merely attends are **not** tasks. Each connector keeps that judgement in one
named, tunable function (`is_actionable_message`, `is_actionable_teams_message`,
`is_actionable_event`) precisely so user corrections can later steer it.

Be strict. A loose heuristic is worse than a missing connector: the point of the
system is to surface the handful of things that matter, and a connector that
emits hundreds of items destroys that.

## Adding a connector

1. Add a member to `SourceKind` in `ontology/types.py`.
2. Add that system's state vocabulary to `STATE_MAP` in `pipeline/ingest.py`.
   Unmapped states fall back to `TRIAGE` — safe, but noisy.
3. **Probe the live server for real tool names** before writing any code.
4. Write the connector; satisfy the protocol, call `assert_read_only` before
   every tool call.
5. Register it in `connectors/__init__.py`.
6. Add recorded fixtures and tests that run **fully offline** — no test may
   require live auth.
7. If the source can be written to, add a `RemediationAction`. Mark it
   `verified=False` until you have confirmed the write semantics against a live
   tenant; unverified actions refuse to execute even when approved.

## Configuration

Some sources need pinning because large tenants expose far too much to scan.
The pattern is to fail with an error that **lists the user's real options**:

```
ado: Set TASK_GRAPH_ADO_ORG to choose an Azure DevOps organization.
     Available: mseng, 1es, msft-skilling, msazure, contosohotelsdev
```

| variable | purpose |
| --- | --- |
| `TASK_GRAPH_ADO_ORG` / `TASK_GRAPH_ADO_PROJECTS` | ADO scope |
| `TASK_GRAPH_PLANNER_PLANS` | Planner plans to query |

## MSX notes

MSX is reached through the `msx-mcp` plugin against
`https://microsoftsales.crm.dynamics.com`, authenticated with `msx_login`, and
requires corporate VPN/SSE. Documented read tools: `get_my_deals`,
`get_my_milestones`, `get_my_hok_activities`, `search_opportunities`,
`get_pipeline_summary`, `dataverse_query`.

**Verified on this machine: MSX is not currently reachable.** `agency mcp msx`
is not a recognised subcommand and no `msx-mcp` plugin is installed. The
connector is implemented and fully tested offline against the documented tool
surface, discovers real tool names at runtime, and reports a specific
remediation distinguishing "plugin not installed" from "not logged in" from
"not on VPN".

Write support is documented only loosely, and opportunity/milestone **state
transitions** are not documented at all. MSX remediation actions are therefore
registered `verified=False` and refuse to execute until confirmed against a live
tenant. Treat MSX as read-and-propose only.
