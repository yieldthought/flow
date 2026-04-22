---
name: flows-compose
description: Use existing flows as first-class tools before inventing a new long-running workflow.
---

# Flows Compose

Use existing flows as first-class tools for long-running subtasks before inventing a new workflow or doing all the work inline.

## Delegate To A Child Flow

When you need to hand off long-running work:

1. Run `flow catalog` to see whether a relevant flow already exists.
2. Before starting anything, check your scratchpad and recent agent state for an already-active child doing the same work. Do not start duplicate children for the same purpose.
3. Start the child with `flow start ...`.
4. Record the returned child agent id, flow name, purpose, and key args in your scratchpad.
5. When the runtime asks for a transition or terminal action and you need the child result before continuing, choose `wait-for-child` and include the exact child id list in `child_ids`.

Example transition response:

```json
{"choice": "wait-for-child", "child_ids": [17], "reason": "waiting for the bootstrap child flow"}
```

Use `choice`, not `action`. No other top-level keys are needed.

## When You Wake

After `wait-for-child`, Flow wakes you in the same state once every named child has finished, stopped, or become unknown.

On wake:

- inspect each child with `flow show <child-id> --json`
- read the child scratchpad path reported by `flow show`
- check the child end state
- copy durable facts into your own scratchpad before moving on
- route failures, stopped children, or unknown children explicitly instead of assuming success

Children are ordinary Flow agents. You can inspect, view, pause, move, stop, or delete them with the normal CLI.

## Waiting Later

You may start a child in one state and wait for it in a later state, but Flow only waits for child ids you explicitly provide. Preserve the child id yourself, usually in your scratchpad, and later return `wait-for-child` with that id.

Flow does not automatically attach child ownership to a state or flow. The wait itself is local to the state where you choose `wait-for-child`: the parent parks and wakes in that same current state.

## If No Useful Flow Exists

If a useful flow does not exist yet:

- create it somewhere on `$FLOW_PATH` such as `~/flows/`
- keep the interface small and clear: description, args help text, explicit end states
- run `flow validate` before you end your turn so later agents can discover it through `flow catalog`
