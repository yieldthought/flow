---
name: flows-compose
description: Use existing Flow 2 workflows as synchronous subprocess tools before inventing a new workflow.
---

# Flows Compose

Use existing flows as first-class subprocess tools before inventing a new
workflow or doing all the work inline.

## Delegate To A Child Flow

When a flow needs another workflow's result:

1. Run `flow catalog` to see whether a relevant flow already exists.
2. Choose a new, explicit scratchpad path for the child.
3. Run the child synchronously with `flow --json --scratchpad FILE CHILD.flow ...`.
4. Read the final JSON event and process exit code.
5. Record the child flow, purpose, terminal state, exit code, scratchpad, and
   thread in the parent scratchpad when they are relevant to later work.

Example:

```bash
flow --json --scratchpad child-bootstrap.md bootstrap.flow --target host
```

The child is an ordinary foreground process. The parent waits for it naturally;
there is no child registration or `wait-for-child` transition.

## When You Wake

After the child exits:

- route the authored terminal exit codes explicitly
- treat 70, 75, 130, and 143 as interruption or recovery outcomes rather than
  ordinary authored results
- keep the child scratchpad and thread when the result is resumable
- inspect a child with `flow inspect CHILD-SCRATCHPAD --json`
- resume it with `flow resume CHILD-SCRATCHPAD --json` when appropriate
- copy durable facts into the parent scratchpad before moving on

The parent does not own resources acquired by the child. Signals and
needs-help do not run cleanup prompts in either process.

## If No Useful Flow Exists

If a useful flow does not exist yet:

- create a `.flow` file somewhere on `$FLOW_PATH`, such as `~/flows/`
- keep the interface small and clear: description, argument help, and explicit
  terminal exit codes
- run `flow validate` before you end your turn so later agents can discover it through `flow catalog`
