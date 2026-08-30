# Flow 2.0

## Contract

Flow 2.0 is a foreground Unix program. `flow FILE.flow [arguments]` runs one
workflow, owns one Codex thread, and exits when the workflow reaches a terminal
state. There is no daemon, tmux session, registry, database, or hidden durable
Flow state.

During development the isolated command is `flow2`; the installed `flow`
command remains the V1 runtime until the migration suite passes.

## Flow files

`.flow` files are YAML documents and may be executable:

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

A terminal state has `exit: N` instead of `end: true`. Flow-defined exit codes
are 0 through 63. A terminal state may omit its prompt to exit immediately.

## Process and state model

The only durable Flow-owned state is a non-hidden Markdown scratchpad. By
default it is created in the invocation directory as
`flow-<flow-file-slug>-<one-based-id>.md`; `--scratchpad FILE` overrides this.
Its managed, human-readable header records the run ID, canonical flow path and
digest, arguments, working directory, host, `CODEX_HOME`, Codex thread, current
state and phase, wait deadline, PID identity, timestamps, and last outcome or
error. The rest of the file belongs to the agent and user. Flow repairs the
managed header after every turn if it was changed or removed.

`flow resume SCRATCHPAD` reacquires an ephemeral lock and resumes the same flow,
state, and Codex thread. It refuses a live lock, a changed flow digest, or a
different host/`CODEX_HOME` by default. `flow inspect SCRATCHPAD --json` reads a
completed or stopped run without maintaining a completed-run registry.

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

On `needs-help`, Flow prints the exact `codex resume THREAD` and
`flow resume SCRATCHPAD` commands. A human may interact with the Codex thread
before resuming Flow.

## Signals and ownership

The first `SIGINT` or `SIGTERM` interrupts the active Codex turn, checkpoints
the scratchpad, terminates Flow's managed local process tree, and exits 130 or
143. A second signal exits immediately. Signals never execute a new prompt.
There is deliberately no `finally`, `on_interrupt`, or implicit cleanup state.
Resource cleanup belongs in normal authored transitions and terminal states;
resources are preserved on needs-help, runtime faults, and signals.

## Output and discovery

TTY output is a concise, timed, coloured transition log. Non-TTY output is
plain. User-visible agent activity may be shown at most once per minute and is
clipped to terminal width; private reasoning is never printed.

`--json` emits JSON Lines on stdout and diagnostics on stderr. Its final event
always includes flow, state, phase, exit code, scratchpad, thread, elapsed time,
and whether the run is resumable.

`flow ps [--json]` discovers same-user live Flow processes and validates their
scratchpad PID/start identity. `flow top` refreshes the same provider. Both are
host-local and intentionally omit completed or stopped flows.

## Composition and remote execution

Child flows initially compose as synchronous subprocesses. Their final JSON
event supplies state, exit code, scratchpad, and thread. Parents do not own or
release resources acquired by children. Remote runs are ordinary foreground
commands over SSH; machine consumers should use `--json`. Resume must happen on
the host and under the `CODEX_HOME` that owns the Codex thread.
