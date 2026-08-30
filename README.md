# flow

> Flow 2.0 is under active development behind the isolated `flow2` command.
> It is a foreground, SDK-backed `.flow` interpreter with scratchpad-only
> persistence. See [the V2 contract](docs/FLOW-2.0.md) and
> [migration status](docs/FLOW-2.0-MIGRATION.md). The existing `flow` command
> remains V1 until the catalog migration and cutover checks are complete.

`flow` runs agents through flowchart-like workflows in the background. You can watch them and help if they need it. An simple flow:

```yaml
flow:
  name: agi-watcher
  mode: workspace-write
  args:
    site:
      help: news site to monitor
  
check-news:
  start: true
  thinking: low
  prompt: Check {{site}} to see if there's a story about AGI being achieved
  transitions:
    - if: there is news that AGI has been acheived
      go: investigate
    - if: there are no stories about AGI being achieved
      wait: 60m
      go: check-news

investigate:
  thinking: xhigh
  prompt: |
    Read the article, comments and any sources you can find.
    Decide whether AGI really has been achieved or if this is just hype.
  transitions:
    - if: AGI really has been acheived
      go: its-over
    - if: AGI has probably not been achieved
      go: check-news

its-over:
  mode: yolo
  prompt: |
    Use pushover to send the user a short summary of the situation.
    Then send another reminding them to go outside, lie on the grass and enjoy the sun.
  end: true
```

Use it like this:

```bash
$ flow start agi-watcher.yaml --site news.ycombinator.com
```

Monitor the situation:

```bash
$ flow catalog
$ flow list
$ flow list --top
$ flow top
Runtime active | uptime 00:18:01 | active agents 3 | total agents 4 | cumulative agent time 00:11:18

agi-watcher
  check-news
    #6  waiting 00:42:32  ~/work/agent-flows  site=news.ycombinator.com
    #7  waiting 00:42:43  ~/work/agent-flows  site=reddit.com/r/locallama
    #8  working 00:00:19  ~/work/agent-flows  site=https://karpathy.github.io
```

Check what a specific agent has been up to:

```bash
$ flow show 6
$ flow show 6 --json
$ flow show 6 --top
agi-watcher in ~/work/agent-flows (started 23:57 on Apr 1 | 0h 0m running, 0h 6m waiting)
State check-news | Substate normal | Phase waiting
Status Waiting until 2026-04-01T22:58:16Z
site: news.ycombinator.com

Events
23:57 on Apr 1 (0h  0m): check-news    started
23:58 on Apr 1 (0h  0m): check-news -> check-news "Checked the live Hacker News front page and relevant HN search results; no current story claims AGI has been achieved."
23:58 on Apr 1 (0h  0m): check-news    wait for 60m until 00:58 on Apr 2
```

View and interact with any codex session directly in your terminal:

```bash
$ flow view 6
```

View many agents in lots of little windows:

```bash
$ flow view --all
```

You have complete control at all times, including pausing and resuming automation for an agent, interrupting it, moving to another state and more. Read the CLI overview for the details.

The main idea is simple:

- a flow is a graph of named states
- each state can give the agent a prompt
- each non-end state has outgoing transitions
- after a turn, the agent chooses the next transition or terminal action in JSON
- the runtime moves the agent, waits, pauses, or asks for help as needed
- every agent is running in a tmux session you can attach to and view or interact with if you have to

`flow` is built for asynchronous work. You start agents, the runtime keeps them moving through a flowchart in the background, and you inspect or intervene only when you want to.

## Principles

- Each agent is always in exactly one state.
- Flows are plain YAML, meant to be easy for both humans and agents to read and write.
- Starting an agent snapshots the flow file. Later edits only affect new agents.
- The runtime is persistent. Agent state lives in `~/.flow` by default.
- Each agent gets its own tmux session and long-lived Codex process.
- Codex uses your normal shared `~/.codex` home, so your usual config, auth, and skills still apply.
- Waiting, pausing, interruption, and recovery are first-class runtime concepts.

## Agent Skills

This repository includes local Codex skills in `.agents/skills/`. They are guidance for agents working with this repo, not runtime configuration.

- `flows-compose` describes the standard pattern for delegating work to child flows with `flow start` and `wait-for-child`.
- `flow-dev` records the repo-specific development commands, test environment, and release steps.

When you maintain flow definitions, prefer putting reusable operating patterns in skills and documentation instead of repeating long mechanical instructions in every YAML prompt. For agents running in another workspace, make sure the relevant skill is available in that workspace or in the user's global Codex skills.

## Requirements

- Python 3.10+
- `tmux`
- a working Codex CLI setup

## Installation

Install the published package:

```bash
python -m pip install flow-like-a-river
```

The name `flow` was taken. Can you believe that?

This installs the `flow` CLI command.

Development setup in a fresh virtual environment:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
pytest
```

## Runtime model

The runtime runs as a detached background process.

- `flow init` starts it if needed
- `flow start ...` also starts it automatically
- `flow restart` gracefully stops it and starts it again
- `flow shutdown` lets agents finish their current turn and then stops the runtime
- `flow shutdown now` kills agents and tmux sessions immediately

State is stored in:

- `~/.flow/runtime.sqlite3`
- `~/.flow/logs/daemon.log`
- `~/.flow/scratchpads/`

You can override the home directory with `FLOW_HOME`.

## Scratchpads

Each agent gets a lightweight scratchpad file at `~/.flow/scratchpads/agent-<id>/scratchpad.md`.

- It is separate from conversation history.
- It is meant for durable working state, not a log or checklist.
- Agents read and edit it directly when there is something genuinely worth preserving across future states or turns.
- The runtime grants sandboxed agents write access to their scratchpad directory with `--add-dir`.

Because editable scratchpads are a core part of the harness, `read-only` is intentionally unsupported. Use `workspace-write` when you want a tighter sandbox than `yolo`.

## Flow files

A flow file has:

- one top-level `flow:` header block
- one block per state, e.g. `my-state:`

Top-level `flow:` fields:

- `name`: flow name
- `description`: human-readable summary shown in UI headers and `flow start <file> --help` (optional)
- `version`: of the flow file format (optional, currently always `1`)
- `path`: initial working directory for new agents (optional, defaults to the current working directory where `flow start` is run)
- `mode`: default Codex permissions mode (optional, defaults to `yolo`, other options are `danger-full-access`, `full-auto`, and `workspace-write`)
- `thinking`: default flow reasoning effort (optional, default `xhigh`, other options are `high`, `medium` and `low`)
- `fast`: default Codex fast mode toggle (optional, default `false`)
- `args`: named CLI arguments for placeholders and their help/defaults (optional if the flow uses no placeholders)

State fields:

- `start: true`: marks a start state (optional)
- `end: true`: marks a terminal state (optional)
- `wait`: default delay before the state runs (optional)
- `prompt`: text sent to the agent on entry (optional; promptless non-end states move directly to transition questions, and promptless end states finish immediately)
- `mode`: per-state mode override (optional default set in `flow:` header)
- `thinking`: per-state thinking override (optional, default set in `flow:` header)
- `fast`: per-state fast mode override (optional, default set in `flow:` header)
- `transitions`: list of outgoing transitions (required unless `end: true`, and forbidden on end states)

Transition fields:

- `if`: natural-language condition, e.g. "the CI tests have all passed"
- `wait`: optional delay before entering the target state, e.g. "10m"
- `go`: target state name

Placeholders like `{{repo}}` can appear in strings. Every placeholder must be declared in `flow.args`, and those declarations become CLI arguments at `flow start` time.

## Catalog and Status JSON

`flow catalog` exposes the flows an agent can discover and reuse.

By default it searches, in order:

- `$FLOW_PATH` if set
- `~/flows`
- `~/.flow/flows`
- `./flows`

Directories are searched recursively for `*.yaml` and `*.yml` files. Only flows that pass validation appear in the default output.

Useful forms:

```bash
flow catalog
flow catalog --format json
flow catalog --broken
flow show 17 --json
```

`flow show --json` emits a compact machine-readable status snapshot, including the current phase, args, scratchpad path, latest event, and child-wait details when the agent is parked on child flows.

### JSON contract

The `flow show --json` shape is fixed. Machine clients should rely on these rules:

- `phase` is one of `enter_state`, `resume_state`, `continue_state`, `evaluate_transition`, `evaluate_terminal`, `submitting`, `working`, `waiting`, `waiting_children`, `needs_help`, `interaction`, `finished`, `stopped`.
- `end_state` is populated only when the agent finished by reaching a flow-declared end state. It is `null` when the agent is still running, paused, or was stopped.
- `terminated_reason` is `"finished"` when the agent reached an end state, `"stopped"` when it was stopped, and `null` otherwise.
- `waiting_on.finished[]` entries carry `status`: `"finished"` when the child reached an end state, `"stopped"` when it was stopped, `"unknown"` when the id does not resolve to any agent. `end_state` in those entries follows the same null-when-not-a-flow-end-state rule.

The transition-evaluation JSON the runtime expects from an agent is also a fixed single shape:

```json
{"choice": "<name>", "reason": "<short explanation>"}
```

When `choice` is `wait-for-child`, also include `"child_ids": [17, 18]`. No other top-level keys are read. In particular, `action` is not accepted as a synonym for `choice`.

## Composing Flows

Flows can launch other flows without any YAML composition syntax.

The intended pattern is:

1. Run `flow catalog` to discover an existing flow that matches the long-running subtask.
2. Check your scratchpad or current agent status for an already-active child doing the same work, and avoid starting duplicates.
3. Run `flow start ...` from the agent turn and capture the child agent id.
4. Record the child id, flow name, purpose, and key args in the parent scratchpad.
5. When the runtime asks for a transition or terminal action and the parent needs the result before continuing, return `wait-for-child` with one or more child ids in `child_ids`.
6. The parent parks in `waiting_children` and wakes in the same state once every named child reaches an end state, is stopped, or becomes unknown.
7. On wake, inspect the child end state and scratchpad path, copy durable facts into the parent scratchpad, and then choose the next transition.

The runtime does not add hierarchy-specific YAML or special child-agent semantics beyond that. Children are still ordinary agents you can inspect, pause, move, stop, or delete with the normal CLI.

Children do not automatically belong to a state or flow. You can start a child in one state and wait for it in a later state, but only if the parent preserves the child id and later supplies it in `child_ids`. The wait is local to the state where `wait-for-child` is chosen: the parent parks and wakes in that same current state.

Codex agents in this repository can also use the local `flows-compose` skill in `.agents/skills/flows-compose/SKILL.md`, which captures this delegation pattern so it does not need to be spelled out in every flow prompt.

Example:

```yaml
flow:
  name: check-ci
  description: Poll a GitHub Actions run until it passes or fails.
  path: ~/project
  args:
    run_url:
      help: GitHub Actions run URL

check:
  start: true
  prompt: |
    Inspect the CI run at {{run_url}}.
  transitions:
    - if: still running
      wait: 10m
      go: check
    - if: passed
      go: notify-pass
    - if: failed
      go: investigate

notify-pass:
  prompt: |
    Send a success notification.
  transitions:
    - go: done

investigate:
  prompt: |
    Investigate the failure and write a short report.
  transitions:
    - go: done

done:
  end: true
```

## How a state runs

When an agent enters a normal non-end state:

1. Flow sends the state prompt to Codex (optionally after a wait period).
2. Codex works until its turn completes.
3. Flow asks Codex to choose one transition in strict JSON.
4. Flow follows that transition.

When an agent enters an end state:

1. Flow waits first if the state defines `wait`.
2. If the end state has no prompt, Flow finishes the agent immediately.
3. If the end state has a prompt, Flow sends it to Codex.
4. Flow then asks Codex to choose one terminal action in strict JSON.
5. Flow either finishes, keeps working in the same end state, or pauses for help.

There are also implicit choices that change the status of an agent without changing its state:

- `keep-working`: stays in the same state and tells Codex to continue working
- `needs-help`: stops automation for this agent and waits for someone to assist it

Prompted end states also have:

- `finish`: ends the agent successfully from the current end state

If a state has no prompt and exactly one unconditional transition, Flow auto-advances without asking Codex anything. This is useful for pure wait states.

## Waiting

`wait` can appear on:

- a state: default delay when entering that state
- a transition: override delay for that specific entry

Internally, waits become an absolute `ready_at` timestamp.

Useful patterns:

- poll every 10 minutes by looping back to the same state with `wait: 10m`
- define a pure wait state with no prompt and one unconditional transition

If you want to cancel a wait early:

```bash
flow wake <agent-id>
```

`wake` only clears the timer. It does not resume an agent that is paused in `interaction` or `needs-help`.

## CLI overview

Check that the installed flow/Codex/tmux integration can run a tiny end-to-end agent flow using your current user environment:

```bash
flow self-test
```

Validate one or more flow files:

```bash
flow validate examples/agi-watcher.yaml examples/ci-notify.yaml
```

Start an agent:

```bash
flow start examples/agi-watcher.yaml --site news.ycombinator.com
```

If the flow has more than one start state:

```bash
flow start my-flow.yaml start-state-name --path ~/work/repo
```

List active and archived agents:

```bash
flow list
flow list agi-watcher
flow list --top
```

Open the live dashboard of active and recently finished agents:

```bash
flow top
flow top agi-watcher
flow top --recent 4h
```

Show one agent in detail:

```bash
flow show 12
flow show 12 --top
```

`flow show` displays:

- flow name and working path
- start time
- total running time
- total waiting time
- scratchpad path
- Codex thread id when known
- a `codex resume ...` hint for finished agents when possible
- args
- a timestamped event log

`flow top` shows the `flow list` summary for active agents plus agents that finished recently, with a `Recent Events` section underneath. By default the recent window is `1h`.

With `--top`, `flow list` and `flow show`, and with `flow top`, the screen clears and redraws every five seconds. Press `space` to refresh immediately and `q` to exit.

If `flow top` is run without a terminal on stdin or stdout, it prints one full dashboard snapshot and exits instead of entering the live redraw loop.

View live tmux sessions:

```bash
flow view 12
flow view 12 15 18
flow view --all
```

With multiple ids, `flow view` opens a tiled tmux dashboard with one read-only pane per agent.

Pause, interrupt, and resume automation:

```bash
flow pause 12
flow interrupt 12
flow resume 12
```

- `flow pause`: pause automation without sending `Ctrl-C`; if Codex is already working on a turn, that turn is allowed to finish naturally
- `flow interrupt`: pause automation and also send `Ctrl-C` to the live Codex session
- `flow resume`: leave `interaction` or `needs-help` and let automation continue

Move or stop an agent:

```bash
flow move 12 investigate
flow resume 12
flow stop 12
flow stop 12 done
```

- `flow move`: move the agent to another state and leave it paused; run
  `flow resume` when you want the target state to start. Moving a finished
  agent resurrects it from the selected state.
- `flow stop`: stop the agent immediately, optionally marking it as a specific
  terminal state

Delete an archived agent entirely:

```bash
flow delete 12
```

Manage the runtime:

```bash
flow init
flow restart
flow shutdown
flow shutdown now
```

## Agent states you will see

Normal runtime state:

- the agent is in a flow state and automation is active

Special substates:

- `interaction`: you paused or interrupted the agent, and automation is paused
- `needs-help`: the agent asked for human help and automation is paused

Other useful runtime phases:

- `waiting`: waiting for `ready_at`
- `submitting`: prompt submission is recorded locally while Codex acknowledges the turn
- `working`: Codex is still working on the current prompt
- `finished`: the agent reached an end state

## Diagnostics

`flow list` includes runtime diagnostics before the state list when relevant.

It can show:

- daemon crash details if the runtime exited with an error
- new runtime warnings and errors since the last time you ran `flow list`
- agent-level `error` and `needs-help` events

This is driven by structured runtime diagnostics, not just raw log scraping.

## Example files

- `examples/agi-watcher.yaml`
- `examples/ci-notify.yaml`

The examples cover:

- placeholders
- polling with `wait`
- success and failure transitions
- push-notification follow-up states
- a simple realistic monitoring flow

## A typical session

Validate a flow:

```bash
flow validate examples/agi-watcher.yaml
```

Start an agent:

```bash
flow start examples/agi-watcher.yaml --site news.ycombinator.com
```

Watch progress:

```bash
flow list
flow list --top
flow show 1
flow show 1 --top
flow view 1
```

Intervene if needed:

```bash
flow pause 1
flow interrupt 1
flow resume 1
flow wake 1
flow move 1 investigate-failure
flow resume 1
```

Restart the runtime after code changes:

```bash
flow restart
```

Stop everything cleanly:

```bash
flow shutdown
```

## Notes

- Reserved state names are `stopped`, `needs-help`, `needs_help`, and `interaction`.
- End states cannot define `transitions`.
- A state can only have one unconditional transition, and it must be last.
- States without transitions must set `end: true` explicitly.
- Relative paths and `~` in `flow.path` are expanded to absolute paths.
- Absolute and relative times in `flow show` use your local timezone for display.
