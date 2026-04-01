# flow

`flow` runs agents through flowchart-like workflows in the background. You can watch them and help if they need it. An simple flow:

```yaml
flow:
  name: agi-watcher
  mode: read-only
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
$ flow list
$ flow list --top
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
- each state has outgoing transitions
- after a turn, the agent chooses the next transition in JSON
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

## Requirements

- Python 3.10+
- `tmux`
- a working Codex CLI setup

## Installation

Install the published package:

```bash
python -m pip install flow-like-a-river
```

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

You can override the home directory with `FLOW_HOME`.

## Flow files

A flow file has:

- one top-level `flow:` header block
- one block per state, e.g. `my-state:`

Top-level `flow:` fields:

- `name`: flow name
- `version`: of the flow file format (optional, currently always `1`)
- `path`: initial working directory for new agents (optional, defaults to the current working directory where `flow start` is run)
- `mode`: default Codex permissions mode (optional, defaults to `yolo`, other options are `danger-full-access`, `full-auto`, `workspace-write`, `read-only`)
- `thinking`: default flow reasoning effort (optional, default `xhigh`, other options are `high`, `medium` and `low`)
- `args`: named CLI arguments for placeholders (optional)

State fields:

- `start: true`: marks a start state (optional)
- `end: true`: marks an end state (optional)
- `wait`: default delay before the state runs (optional)
- `prompt`: text sent to the agent on entry (optional, state moves directly to transition questions if empty)
- `mode`: per-state mode override (optional default set in `flow:` header)
- `thinking`: per-state thinking override (optional, default set in `flow:` header)
- `transitions`: list of outgoing transitions (optional only if `end: true`)

Transition fields:

- `if`: natural-language condition, e.g. "the CI tests have all passed"
- `wait`: optional delay before entering the target state, e.g. "10m"
- `go`: target state name

Placeholders like `{{repo}}` can appear in strings. They become CLI arguments at `flow start` time.

Example:

```yaml
flow:
  name: check-ci
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

When an agent enters a normal state:

1. Flow sends the state prompt to Codex (optionally after a wait period).
2. Codex works until its turn completes.
3. Flow asks Codex to choose one transition in strict JSON.
4. Flow follows that transition.

There are also two implicit choices that change the status of an agent without changing its state:

- `keep_working`: stays in the same state and tells Codex to continue working
- `needs_help`: stops automation for this agent and waits for someone to assist it

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

`wake` only clears the timer. It does not resume an agent that is paused in `interaction` or `needs_help`.

## CLI overview

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
- args
- a timestamped event log

With `--top`, `flow list` and `flow show` clear and redraw the screen every five seconds. Press `space` to refresh immediately and `q` to exit.

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
- `flow resume`: leave `interaction` or `needs_help` and let automation continue

Move or stop an agent:

```bash
flow move 12 investigate
flow stop 12
flow stop 12 done
```

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
- `needs_help`: the agent asked for human help and automation is paused

Other useful runtime phases:

- `waiting`: waiting for `ready_at`
- `working`: Codex is still working on the current prompt
- `finished`: the agent reached an end state

## Diagnostics

`flow list` includes runtime diagnostics before the state list when relevant.

It can show:

- daemon crash details if the runtime exited with an error
- new runtime warnings and errors since the last time you ran `flow list`
- agent-level `error` and `needs_help` events

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

- Reserved state names are `stopped`, `needs_help`, and `interaction`.
- End states cannot define `wait`.
- A state can only have one unconditional transition, and it must be last.
- Relative paths and `~` in `flow.path` are expanded to absolute paths.
- Absolute and relative times in `flow show` use your local timezone for display.
