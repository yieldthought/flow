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
is `flow`.

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
- `mode`, `thinking`, and `fast`: optional per-state overrides
- `transitions`: ordered `if` / `wait` / `go` routes to other states
- `exit: N`: makes the state terminal and defines its process exit code

Every non-terminal state needs a prompt and at least one transition. A terminal
state uses `exit: N`, cannot have transitions, and may omit its prompt to exit
immediately. Flow-authored exit codes are 0 through 63.

Validate before running:

```bash
flow validate review-pr.flow
flow validate flows/*.flow
```

## Running and resuming

```bash
flow [--json] [--scratchpad FILE] FILE.flow [flow arguments]
flow resume SCRATCHPAD [--json]
flow inspect SCRATCHPAD [--json]
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

When a flow exits because it needs help, it prints both commands needed to
continue:

```bash
codex resume THREAD
flow resume SCRATCHPAD
```

A human or agent can resume the Codex thread, resolve the problem, and then
resume Flow from the same checkpoint.

## Output

Human terminal output is a concise timed transition log with functional pastel
colours. Redirected output is plain. `NO_COLOR` disables colour explicitly.

`--json` emits JSON Lines on stdout and keeps diagnostics on stderr. Its final
event includes the flow, state, phase, exit code, scratchpad, thread, elapsed
time, and whether the run can be resumed. This is the stable interface for
scripts, parent flows, remote runners, and agents.

```bash
flow --json review-pr.flow --pr 1234
```

Flow may print a clipped one-line summary of visible agent activity at most
once per minute. It does not print private reasoning.

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
```

`flow top` uses an alternate terminal screen and refreshes in place. Press `q`
or Escape to quit. Discovery is host-local and intentionally omits completed
flows.

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
[`Flow 1 archived guide`](docs/FLOW-1.md).
