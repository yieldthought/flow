---
name: flows-compose
description: Use existing flows as first-class tools before inventing a new long-running workflow.
---

# Flows Compose

When you need to hand off long-running work, first run `flow catalog` to see whether a relevant flow already exists.

If it does:

- start it with `flow start ...`
- note the child agent id
- when the runtime asks for a transition or terminal action, choose `wait-for-child` and provide the child id list in `child_ids`
- when you wake, inspect the child end state and scratchpad before deciding what to do next

If a useful flow does not exist yet:

- create it somewhere on `$FLOW_PATH` such as `~/flows/`
- keep the interface small and clear: description, args help text, explicit end states
- run `flow validate` before you end your turn so later agents can discover it through `flow catalog`
