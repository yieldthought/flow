# Flow 2 runtime contract

## Contract

Flow 2.0 is a foreground Unix program. `flow FILE.flow [arguments]` runs one
workflow, owns one Codex thread, and exits when the workflow reaches a terminal
state. There is no daemon, tmux session, registry, database, or hidden durable
Flow state.

Flow 2 is the runtime behind the installed `flow` command. The legacy Flow 1
runtime is available temporarily as `flow1`.

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

`flow chart FILE.flow` validates the definition, renders it through the
Graphviz `dot` executable into a standalone temporary HTML page, and opens the
page in the platform's default browser. The named-output form writes the same
artifact without opening it:

```bash
flow chart FILE.flow --output NAME.html
flow chart FILE.flow --theme light --output NAME-light.html
```

The chart embeds its SVG, has no network dependency, distinguishes start,
wait, successful terminal, and nonzero terminal behavior, and provides a state
inspector for full prompts and transition conditions. Selecting a state keeps
the stable state colours on its inbound and outbound relationships and greys
out unrelated nodes and edges. Inbound edges match the selected state; outbound
edges match their respective destination states. Hovering a transition label
shows its complete, untruncated condition.
The graph runs primarily top to bottom, uses browser-level scrolling, and
places its two-column inspector below the graph. Graph-only zoom controls start
at a compact `100%`; `Fit width` scales the graph to the browser viewport.
The initial overview derives a stable pastel colour from each state name and
uses that colour for every edge entering the state. Clicking the graph
background restores that overview. Inspector transition targets use the same
destination-state colours and navigate directly to those states.
Graphviz is an optional external dependency used only by this command. The
default theme is `dark`; `--theme light` changes both the page and embedded
graph palette.

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

`flow chat SCRATCHPAD` acquires the same ephemeral lock and runs `codex resume`
interactively for the recorded thread. It does not run a Flow turn, change
state, or evaluate transitions. Both completed and resumable stopped runs may
be opened, but a live Flow or another chat owns the lock and is refused. The
lock remains held for the whole Codex session, so chat must exit before
`flow resume` can continue the workflow. If the recorded working directory was
a removed temporary worktree, chat uses the original invocation directory, or
the scratchpad directory as a final fallback, and reports that substitution.

While the process owns `<scratchpad>.lock`, that already-required ephemeral
file also contains the structured events emitted during the current invocation.
It is deleted when the process exits and is not checkpoint state. Losing it
loses only live output history; it cannot prevent inspection or resume.

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

On `needs-help`, Flow prints the exact `flow chat SCRATCHPAD` and
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
plain. Elapsed stamps show whole minutes with leading zero units omitted and
expand to hours and days as required. Wait deadlines are rendered in the CLI's
local timezone with a compact duration, while JSON retains exact elapsed
seconds and the UTC timestamp. Completed user-visible
commentary from the main Codex thread is batched
for one lazy, ephemeral `gpt-5.6-luna` thread. Luna receives only fresh public
text, limited recent public context, the run objective and arguments, the
current state's transition criteria, and the last few published summaries. It
assesses at most once per minute and emits only a standalone update that
materially changes what an observer should understand about progress, risk,
blockers, or required action. New technical or administrative facts are not
sufficient by themselves, and silence is preferred to a cryptic summary.
Secret-like argument values are redacted before assessment. Without new
commentary there is no Luna request. Activity summaries are observational,
carry
`source: "luna-summary"` in JSON, and are clipped to terminal width for human
output after a 100-character limit. An overlong Luna result is discarded rather
than semantically truncated. Summarization failure, cancellation,
or a result arriving after its work turn cannot affect workflow state or exit
status. Private reasoning is never sent or printed.

`--json` emits JSON Lines on stdout and diagnostics on stderr. Its final event
always includes flow, state, phase, exit code, scratchpad, thread, elapsed time,
and whether the run is resumable.

`flow watch SCRATCHPAD [--json]` replays the current invocation's lock journal
and follows it until the Flow exits or the observer detaches. It never signals
or otherwise controls the observed Flow. Detached launchers may retain the same
JSON Lines in a sibling `.jsonl` file, which `watch` can replay after completion.

`flow ps [--json]` discovers same-user live Flow processes and validates their
scratchpad PID/start identity. `flow top` refreshes the same provider and lets a
TTY user select a row with up/down, open its watch view with Enter/right, and
return with Escape/left. Both are host-local and intentionally omit completed
or stopped flows from the list view.

## Composition and remote execution

Child flows initially compose as synchronous subprocesses. Their final JSON
event supplies state, exit code, scratchpad, and thread. Parents do not own or
release resources acquired by children. Remote runs are ordinary foreground
commands over SSH; machine consumers should use `--json`. Resume must happen on
the host and under the `CODEX_HOME` that owns the Codex thread.

## Examples

Executable Flow 2 examples live in `examples/`. Archived Flow 1 definitions
live separately in `flow1/examples/`.

- `examples/agi-watcher.flow`: arguments, polling, waits, and a terminal action
- `examples/ci-notify.flow`: success and failure routing into one successful exit
- `examples/hello-child.flow`: a minimal successful child
- `examples/hello-parent.flow`: synchronous child composition through JSON Lines
- `examples/sdk-self-test.flow`: harmless real SDK integration check

From the repository root:

```bash
flow validate examples/*.flow
flow examples/hello-child.flow
flow examples/hello-parent.flow
```
