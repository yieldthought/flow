# Flow

Flow is a foreground workflow interpreter for Codex agents. A `.flow` file is
an executable graph of prompts and transitions:

```bash
flow review-pr.flow --pr 1234
```

One command runs one flow, owns one Codex thread, and exits with the code chosen
by the flow. There is no daemon, tmux session, registry, database, or hidden
durable Flow state.

## Quick start

```yaml
#!/usr/bin/env flow
flow:
  name: review-pr
  version: 2
  path: .
  mode: workspace-write
  thinking: high
  args:
    pr:
      help: Pull request number

review:
  start: true
  prompt: Review PR {{pr}} and fix material issues.
  transitions:
    - if: The review and fixes are complete.
      go: complete
    - if: Progress requires human input.
      go: blocked

complete:
  exit: 0
  prompt: Verify the final result and leave a concise summary in the scratchpad.

blocked:
  exit: 2
  prompt: Record the concrete blocker and the smallest useful next action.
```

Run it through the interpreter:

```bash
flow review-pr.flow --pr 1234
```

Or make it executable and run it directly:

```bash
chmod +x review-pr.flow
./review-pr.flow --pr 1234
```

Flow writes a non-hidden scratchpad such as `flow-review-pr-1.md` in the
invocation directory. Its header holds the checkpoint needed to resume the
same state and Codex thread; the Markdown body belongs to the agent and user.

## Install

Flow requires Python 3.10+ and a working Codex login.

```bash
python -m pip install flow-like-a-river
```

The package name differs because `flow` was already taken on PyPI. The command
is `flow`. The optional `flow chart` command also requires the Graphviz `dot`
executable on `PATH`.

For development:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
pytest
```

## Flow files

`.flow` files are YAML documents with one top-level `flow:` block and one block
per state.

The `flow:` block supports:

- `name`: required flow name
- `version`: format version, currently `2` and defaulting to `2`
- `description`: optional help text
- `path`: working directory, defaulting to the invocation directory
- `mode`: `yolo`, `danger-full-access`, `full-auto`, or `workspace-write`
- `thinking`: `none`, `minimal`, `low`, `medium`, `high`, or `xhigh`
- `fast`: optional Codex fast-mode toggle
- `model`: optional Codex model override
- `args`: named command-line arguments used by `{{placeholders}}`

A state supports:

- `start: true`: marks the one start state
- `prompt`: work to send to Codex
- `wait`: delay before the state runs, such as `30s`, `10m`, or `2h`
- `mode` and `fast`: optional per-state overrides
- `thinking`: accepted as a compatibility override, but discouraged; new and
  maintained flows should set one reasoning effort in the top-level `flow:`
  block and leave it unchanged across states
- `transitions`: ordered `if` / `wait` / `go` routes to other states
- `exit: N`: makes the state terminal and defines its process exit code

Every non-terminal state needs a prompt and at least one transition. A terminal
state uses `exit: N`, cannot have transitions, and may omit its prompt to exit
immediately. Flow-authored exit codes are 0 through 63.

Changing reasoning effort between turns invalidates the OpenAI model's reusable
prefill cache and usually increases total cost, even when a terminal or polling
state appears simple. Choose the budget for the workflow as a whole.

Validate before running:

```bash
flow validate review-pr.flow
flow validate flows/*.flow
```

## Running and resuming

```bash
flow [--json] [--scratchpad FILE] FILE.flow [flow arguments]
flow resume SCRATCHPAD [--json]
flow chat SCRATCHPAD
flow chart FILE.flow [-o OUTPUT.html] [--theme dark|light]
flow inspect SCRATCHPAD [--json]
flow watch SCRATCHPAD [--json]
```

By default the scratchpad is named
`flow-<flow-file-slug>-<one-based-id>.md`. `--scratchpad FILE` chooses an
explicit path.

The managed scratchpad header records the flow path and digest, arguments,
working directory, host, `CODEX_HOME`, Codex thread, state, phase, wait
deadline, process identity, timestamps, and last outcome. Flow repairs that
header after every turn if the agent changed or removed it.

`flow resume` continues the same flow, state, and Codex thread. It refuses a
live run, changed flow file, different host, or different `CODEX_HOME` by
default. Deliberate recovery options are available in command help:

```bash
flow resume --help
```

`flow chat SCRATCHPAD` opens the scratchpad's Codex thread for interactive
questions without advancing the Flow or evaluating a transition. It works for
completed, needs-help, interrupted, and otherwise stopped runs, but refuses a
scratchpad currently owned by a live Flow process. The command holds that same
ownership lock while `codex resume` is open, preventing `flow resume` from
driving the thread concurrently. When a removed temporary worktree was the
original working directory, chat falls back to the invocation directory and
prints the substitution. Exit chat before running `flow resume`.

When a flow exits because it needs help, it prints the commands for talking to
the stopped agent and then continuing the workflow:

```bash
flow chat SCRATCHPAD
flow resume SCRATCHPAD
```

A human can ask questions or resolve the problem in the Codex thread, exit the
chat, and then resume Flow from the same checkpoint.

## Flow charts

```bash
flow chart review-pr.flow
flow chart review-pr.flow --output review-pr-chart.html
flow chart review-pr.flow --theme light --output review-pr-light.html
```

The first form renders a standalone HTML chart in the system temporary
directory and opens it in the platform's default browser. `--output` (or `-o`)
writes to the named location and does not open a viewer. The page embeds its
SVG and has no network dependency. Start, wait, successful exit, and nonzero
exit states use distinct functional colours; transition waits use dashed
edges. The graph is laid out primarily from top to bottom and uses the browser's
ordinary page scrolling. Graph zoom starts at a compact `100%`; the controls
zoom the graph independently, and `Fit width` fits it to the browser viewport.
Select a state to inspect its complete prompt, settings, and transition
conditions without crowding the graph. Each state has a deterministic pastel
colour and every incoming edge uses its destination state's colour. Selecting
a state preserves those colours for its inbound and outbound relationships and
greys out everything unrelated. Inbound edges therefore match the selected
state, while outbound edges match their respective destination states. Click
the graph background to return to the fully coloured overview.
Hovering a transition label shows its complete, untruncated condition.
The full-width inspector below the graph
places the prompt beside its transitions; transition targets navigate directly
to their state and use that destination state's colour. Charts use the dark
theme by default; `--theme light` produces the corresponding light palette.

## Output

Human terminal output is a concise timed transition log with functional pastel
colours. Redirected output is plain. `NO_COLOR` disables colour explicitly.
Elapsed stamps omit seconds and leading zero units, growing from `[     5m]` to
`[ 3h 14m]` and `[1d  0h 17m]` as needed.
Wait events show their deadline in the CLI's local timezone followed by a
compact duration, for example `16:13 on Sep 1 (2h 30m)`.

`--json` emits JSON Lines on stdout and keeps diagnostics on stderr. Its final
event includes the flow, state, phase, exit code, scratchpad, thread, elapsed
time, and whether the run can be resumed. This is the stable interface for
scripts, parent flows, remote runners, and agents.

While a Flow process is live, the same structured events are appended to its
existing `<scratchpad>.lock` file. `flow watch SCRATCHPAD` replays that
invocation's output and follows new events. On a TTY, `q`, Escape, or left arrow
detaches the watcher without signalling the Flow. `--json` emits the replayed
events as JSON Lines. The lock journal is deleted when the process exits and is
never required to inspect or resume the scratchpad; detached launchers may keep
their separately redirected JSONL output for later replay.

```bash
flow --json review-pr.flow --pr 1234
```

Flow uses one lazy, ephemeral `gpt-5.6-luna` thread to assess user-visible
agent commentary for useful activity updates. Luna is called only after the
main Codex thread completes new public commentary, and no more than once per
minute; the interval is a rate ceiling, not a heartbeat. It sees the fresh
text, limited recent context, the run objective and arguments, the current
state's transition criteria, and the last few published summaries. It emits
only standalone updates that materially change what an observer should
understand about progress, risk, blockers, or required action. New technical
or administrative facts are not sufficient by themselves, and silence is
preferred to a cryptic summary. Human summaries are limited to 100 characters
and clipped further to terminal width; secret-like argument values are
redacted, and an overlong Luna result is discarded rather than truncated. JSON
activity events use
`source: "luna-summary"`. Summarization failures and late results are discarded
and cannot affect Flow state or exit status. Private reasoning is never sent or
printed.

## Exit codes

| Code | Meaning |
| ---: | --- |
| 0-63 | Flow-authored terminal outcome |
| 64 | Invalid command-line invocation |
| 65 | Invalid flow or scratchpad data |
| 70 | Runtime or Codex failure |
| 75 | Resumable `needs-help` outcome or live-run conflict |
| 130 | Interrupted by `SIGINT` |
| 143 | Terminated by `SIGTERM` |

Choose terminal exit codes as part of the flow's public contract. Use `0` for
successful outcomes and distinct non-zero values for outcomes callers need to
route differently.

## Signals and resource ownership

The first `SIGINT` or `SIGTERM` interrupts the active Codex turn, checkpoints
the scratchpad, terminates Flow's managed local process tree, and exits 130 or
143. A second signal exits immediately.

Signals never execute a new prompt. Flow deliberately has no implicit
`finally`, `on_interrupt`, or cleanup state: when someone presses Ctrl-C, Flow
does not ask an agent to take another potentially destructive action. Flows
that acquire resources should release them through normal authored transitions
and terminal states. Interrupted and needs-help runs preserve their checkpoint
for deliberate recovery.

## Process discovery

Flow itself is stateless, but live processes can be discovered from the host's
process table and their scratchpad headers:

```bash
flow ps
flow ps --json
flow top
flow watch SCRATCHPAD
```

`flow top` uses an alternate terminal screen and refreshes in place. Use up and
down arrows to select a flow, then Enter or right arrow to open its live output.
In the watch view, up/down scroll and left arrow or Escape returns to the list;
`q` quits from either view. Selection is preserved across refreshes. Discovery
is host-local and intentionally omits completed flows.

## Catalog

```bash
flow catalog
flow catalog --json
flow catalog ~/flows ./project-flows
```

Without explicit paths, the catalog searches `$FLOW_PATH` when set, otherwise
`~/flows` and `./flows`, recursively. Only `.flow` files are considered.

## Composing flows

Run a child flow as an ordinary synchronous subprocess, preferably with JSON
output. The child's final event supplies its state, exit code, scratchpad, and
Codex thread.

```bash
flow --json child.flow --target value
```

The parent interprets the child exit code and final event like any other Unix
caller. A parent does not own resources acquired by a child, and Flow does not
add a hidden parent/child registry. See
[`examples/hello-parent.flow`](examples/hello-parent.flow) for a complete
example.

Remote execution uses the same contract:

```bash
ssh HOST 'cd WORKDIR && flow --json FILE.flow ARGS...'
```

Resume must happen on the host and under the `CODEX_HOME` that owns the Codex
thread.

## Examples

- [`examples/agi-watcher.flow`](examples/agi-watcher.flow): arguments, polling,
  waits, and a terminal action
- [`examples/ci-notify.flow`](examples/ci-notify.flow): success and failure
  routing
- [`examples/hello-child.flow`](examples/hello-child.flow): minimal child flow
- [`examples/hello-parent.flow`](examples/hello-parent.flow): synchronous JSON
  composition
- [`examples/sdk-self-test.flow`](examples/sdk-self-test.flow): real SDK smoke
  test

```bash
flow validate examples/*.flow
flow examples/hello-child.flow
flow examples/hello-parent.flow
```

The detailed runtime invariants are recorded in the
[`Flow 2 contract`](docs/FLOW-2.0.md).

## Flow 1 migration and compatibility

Flow 2 is the current and only recommended way to write and run new flows.
Version 2.0 changes the primary command from the daemon-based Flow 1 CLI to the
foreground `.flow` interpreter:

| Purpose | Current command |
| --- | --- |
| Run a Flow 2 workflow | `flow FILE.flow ...` |
| Use the legacy Flow 1 runtime temporarily | `flow1 ...` |

Flow 1 used `.yaml` files, a persistent server, tmux sessions, numeric agent
IDs, and state under `~/.flow`. Those facilities remain available through the
temporary `flow1` compatibility command, but they are no longer developed as
the primary product. Do not build new automation around `flow1`.

See [Migrating from Flow 1](docs/FLOW-2.0-MIGRATION.md) for the file and command
changes. The complete pre-2.0 documentation is preserved as the
[`Flow 1 archived guide`](flow1/README.md).
