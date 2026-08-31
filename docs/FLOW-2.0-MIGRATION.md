# Migrating from Flow 1

Flow 2 replaces the persistent daemon/tmux runtime with a foreground Unix
process. New workflows should use Flow 2 exclusively.

## Command cutover

After installing Flow 2.0:

| Command | Runtime |
| --- | --- |
| `flow FILE.flow ...` | Current Flow 2 runtime |
| `flow1 ...` | Temporary Flow 1 compatibility runtime |

The development-only `flow2` command no longer exists in published package
metadata. Replace it with `flow` in scripts and shebangs.

Flow 1 commands still work when prefixed with `flow1`:

```bash
flow1 start old-flow.yaml --arg value
flow1 list
flow1 show 17
flow1 view 17
flow1 shutdown
```

Flow 1 requires its existing daemon, tmux, SQLite state, and `~/.flow`
directories. It is retained only to finish or inspect old runs while workflows
are ported. Do not start new integrations against it.

## File format changes

Flow 2 files use the `.flow` extension and `version: 2`. A `.flow` file may be
executable:

```yaml
#!/usr/bin/env flow
flow:
  name: example
  version: 2
```

The important schema changes are:

- replace `.yaml` or `.yml` with `.flow`
- replace terminal `end: true` with an explicit `exit: N`
- assign `0` to successful terminal outcomes and distinct non-zero codes to
  outcomes callers need to route
- run the file directly with `flow FILE.flow`; there is no `start` subcommand
- treat each invocation as one foreground process rather than a registered
  background agent

Most `flow:`, state prompt, transition, wait, argument, model, mode, thinking,
and fast fields port directly.

Before:

```yaml
flow:
  name: check-ci

check:
  start: true
  prompt: Check CI.
  transitions:
    - if: CI passed.
      go: success
    - if: CI failed.
      go: failure

success:
  end: true

failure:
  end: true
```

After:

```yaml
#!/usr/bin/env flow
flow:
  name: check-ci
  version: 2

check:
  start: true
  prompt: Check CI.
  transitions:
    - if: CI passed.
      go: success
    - if: CI failed.
      go: failure

success:
  exit: 0

failure:
  exit: 2
```

## Operational changes

Flow 1 identified runs by numeric agent ID and kept durable state in a daemon
database. Flow 2 identifies a run by its visible Markdown scratchpad:

```bash
flow check-ci.flow
flow inspect flow-check-ci-1.md
flow resume flow-check-ci-1.md
```

`flow ps` and `flow top` discover only live local processes. Completed runs are
not retained in a hidden registry; their scratchpads are the durable record.

A needs-help result exits 75 and prints the exact `codex resume` and
`flow resume` commands. Resolve the issue in the Codex thread, then resume Flow
from the same scratchpad.

Ctrl-C and SIGTERM checkpoint and exit. They do not run cleanup prompts.
Resource release must be part of normal flow transitions and terminal states.

## Composition changes

Flow 1 parents could launch registered children and return `wait-for-child`.
Flow 2 composition is ordinary synchronous process composition:

```bash
flow --json child.flow --arg value
```

Read the child's final JSON event and exit code. Preserve its scratchpad and
thread when the result is resumable. There is no hidden child registry or
resource ownership transfer.

## Porting checklist

1. Copy the workflow to a `.flow` file and set `version: 2`.
2. Replace every `end: true` with an intentional `exit: N`.
3. Change `flow start FILE.yaml` to `flow FILE.flow`.
4. Change `flow2` development references and shebangs to `flow`.
5. Replace numeric-agent inspection with scratchpad-based `inspect` and
   `resume`.
6. Replace `wait-for-child` composition with a synchronous `flow --json`
   subprocess.
7. Confirm Ctrl-C preserves resources safely; do not add interrupt cleanup
   prompts.
8. Run `flow validate FILE.flow`.
9. Exercise success, authored failure, needs-help, resume, and signal paths.

## Compatibility window

`flow1` preserves the pre-2.0 runtime for a short migration window. It is not a
second supported design direction and may be removed after old runs and files
have been retired.

The complete historical documentation is available in the
[Flow 1 archived guide](FLOW-1.md). The current behavior is defined by the
[main README](../README.md) and [Flow 2 runtime contract](FLOW-2.0.md).
