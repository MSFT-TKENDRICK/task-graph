# Connectors

A connector turns one source system into `SourceItem` records. Everything
downstream — ingest, dedupe, priority, remediation — is source-agnostic, so
adding a system is a self-contained change.

## Shipped connectors

| kind | transport | auth |
| --- | --- | --- |
| `github` | `gh` CLI subprocess | existing `gh` login |
| `ado` | `agency mcp ado` over stdio | ambient domain-joined Entra |
| `mail` | `agency mcp mail` over stdio | ambient domain-joined Entra |

Designed for, not yet implemented: `teams`, `calendar`, `planner`, `msx`.

## Why MCP subprocesses

Microsoft's Agency CLI ships first-party MCP servers that already hold the
signed-in user's Entra session. Running `agency mcp <name>` starts one on stdio.
By acting as an MCP *client*, this project inherits that auth and never touches
a credential — no tokens, no refresh flows, no secrets in config.

Verified server names available through Agency:

```
ado  bluebird  calendar  cloudbuild  es-chat  icm  kusto  m365-copilot
m365-user  mail  msft-learn  planner  security-context  teams  word  workiq
```

`AgencyMcpClient` wraps this. It resolves `agency` via `shutil.which`, reports
unavailability instead of crashing when it is missing, exposes a synchronous
facade over the async MCP SDK, and always terminates the subprocess.

It discovers tool names at runtime via `list_tools()` rather than hardcoding
them, because the exact tool surface varies between Agency versions.

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
github:issue:owner/repo#123
github:pr:owner/repo#45
github:discussion:owner/repo#7
ado:workitem:12345
mail:<message-id@corp>
```

Use the provided builders (`github_issue_uri`, `ado_workitem_uri`, `mail_uri`, …)
rather than formatting strings by hand.

### `external_refs` drives cross-source dedupe

Populate it with every identifier the record mentions — `AB#12345`, `#123`,
work-item URLs, PR links. `extract_external_refs` does this for free text.

This is the highest-value field a connector produces. A GitHub issue whose body
says `AB#12345` and ADO work item 12345 will auto-link on that evidence alone,
regardless of how differently they are worded. Without it, the same pair only
reaches a *proposal* on textual similarity.

### `source_state` must be preserved verbatim

Keep the source system's own state string. Ingest maps it onto our lifecycle via
`STATE_MAP`, but remediation needs the original vocabulary to perform a
transition ("Active → Resolved").

## Adding a connector

1. Add a member to `SourceKind` in `ontology/types.py`.
2. Add that system's state vocabulary to `STATE_MAP` in `pipeline/ingest.py`.
   Unmapped states fall back to `TRIAGE`, which is safe but noisy.
3. Write the connector in `connectors/`, subclassing nothing — just satisfy the
   protocol. For an Agency-backed source this is mostly `AgencyMcpClient` plus a
   mapping function.
4. Register it with `register_connector`.
5. Add a recorded fixture under `tests/fixtures/` and a test that maps it to
   `SourceItem`s. **Do not write a test that needs live auth** — everything in
   the suite runs offline.
6. If the source can be written to, add a `RemediationAction` for it. Mark it
   `verified=False` until you have confirmed the write semantics against a live
   tenant; unverified actions refuse to execute even when approved.

## MSX notes

MSX is reached through the `msx-mcp` plugin against
`https://microsoftsales.crm.dynamics.com`, authenticated with `msx_login`, and
requires corporate VPN/SSE. Its documented read tools include `get_my_deals`,
`get_my_milestones`, `get_my_hok_activities`, `search_opportunities` and
`dataverse_query`.

Write support is documented only loosely, and opportunity/milestone **state
transitions** are not documented at all. The `advance_milestone` action is
therefore registered as `verified=False` and will refuse to execute until its
semantics are confirmed against a live tenant. Treat MSX as read-and-propose
only.
