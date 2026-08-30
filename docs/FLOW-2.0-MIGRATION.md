# Flow 2.0 Migration

## Proven

- The legacy `flow` command and all 202 V1 tests remain unchanged.
- `flow2` runs `.flow` files in the foreground through `openai-codex` 0.147.
- A real SDK run created a thread, streamed activity, chose a structured
  transition, checkpointed one Markdown scratchpad, and returned authored exit
  0.
- A real SIGINT during transition evaluation returned 130 without executing a
  transition. `flow2 resume` then reused the same thread and completed without
  repeating state work.
- `flow2 ps --json` found a live shebang-invoked run from its process and
  scratchpad identity. It disappeared on exit.
- AutoDebug's foreground runner and Codex thread handoff tests pass unchanged.
  AutoDebug remains an ordinary subprocess; Flow does not absorb its state or
  cleanup policy.

## Catalog ports

V1 YAML files remain active and untouched. These V2 files are side by side and
are ignored by V1's YAML-only catalog:

| Flow | Exit contract |
| --- | --- |
| `pr-to-ci-result` | success/inherited 0; branch failure 2; harness noise 3 |
| `local-branch-to-ready-pr` | success 0; too complex 2 |
| `ci-issue-to-ready-pr` | success 0; too complex 2 |
| `gh-issue-to-ready-pr` | success 0; too complex 2; blocked 3 |
| `ci-log-to-issue` | created/updated-existing 0; blocked 2 |
| `ci-run-to-notification` | done 0 |
| `ci-runs-to-pr-comment` | success-no-post/posted 0; failure 2; blocked 3 |
| `deepseek-ci-to-triage-report` | report-to-slack 0 |
| `pr-to-model-bounty-review` | pending-review-created 0; blocked 2 |
| `repo-issues-to-autodebug-case` | success/no-new-issues 0; new-hard-case 1; blocked 2 |
| `repo-issues-to-autotriage-case` | success/no-new-issues 0; new-hard-case 1; blocked 2 |
| `mark-away-continuity` | cycle-complete/final-handoff 0; blocked 2 |

Every V1 YAML file remains active and untouched; each V2 sibling is at
`~/flows/<name>.flow`. All 12 validate and are executable. The three CI-parent
flows run `pr-to-ci-result` synchronously with `flow2 --json`, record the final
state, exit, scratchpad, and thread, and treat 70/75/130/143 as resumable
intervention. `mark-away-continuity` now performs one scheduled cycle per
process and runs selected children synchronously and sequentially.

## Remote contract

No remote runner is required. Install the same Flow build and Codex auth on the
remote host, then run an ordinary foreground command over SSH:

```bash
ssh HOST 'cd WORKDIR && flow --json FILE.flow ARGS...'
```

The JSON final event is the caller contract. `flow ps` and `flow top` are
host-local. Resume must run on the host and under the `CODEX_HOME` named in the
scratchpad. Remote resource acquisition and release stay in authored states;
signals and needs-help do not run cleanup prompts.

## Cutover gates

1. Exercise one resource-owning IRD flow and one AutoDebug flow without
   performing destructive cleanup on interruption.
2. Exercise synchronous child signal propagation on a real parent/child pair.
3. Install `flow2` on a remote host and verify SSH JSON output plus same-host
   resume.
4. Route `flow` to V2, change development shebangs from `flow2` to `flow`, and
   retain V1 under an explicit compatibility command for one release.
