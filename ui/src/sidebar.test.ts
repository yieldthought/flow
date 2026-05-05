import { describe, expect, it } from "vitest";

import { buildSidebarFlowRows } from "./App";
import type { EditorFileEntry, RuntimeTopSnapshot } from "./types";

const file: EditorFileEntry = {
  path: "/flows/demo.yaml",
  root: "/flows",
  name: "demo",
  description: "Editable demo.",
  valid: true,
  errors: [],
  warnings: [],
  stateCount: 2,
  transitionCount: 1,
  updatedAt: "2026-05-04T10:00:00Z",
};

const top: RuntimeTopSnapshot = {
  runtime: {
    active: true,
    pid: 123,
    started_at: "2026-05-04T10:00:00Z",
    heartbeat_at: "2026-05-04T10:00:01Z",
    uptime_seconds: 60,
    diagnostics: [],
  },
  recent: { window: "1h", cutoff: "2026-05-04T09:00:00Z", seconds: 3600 },
  flows: [
    {
      name: "demo",
      description: "Runtime demo.",
      path: "/flows/demo.yaml",
      counts: { waiting: 0, working: 1, paused: 0, needs_help: 0 },
      active_count: 1,
      recent_count: 1,
      agents: [
        {
          id: 7,
          flow_name: "demo",
          current_state: "check",
          status: "working",
          timer_seconds: 120,
          args: {},
          display_args: {},
          state_name: "check",
          substate: "normal",
          phase: "waiting_turn",
          cwd: "/tmp",
          ready_at: "",
          ended_at: "",
          latest_message: "Created https://github.com/example/repo/issues/1",
          source_path: "/flows/demo.yaml",
          created_at: "2026-05-04T10:00:00Z",
          updated_at: "2026-05-04T10:01:00Z",
        },
      ],
    },
    {
      name: "runtime-only",
      description: "Only in the runtime.",
      path: "/tmp/runtime-only.yaml",
      counts: { waiting: 0, working: 0, paused: 0, needs_help: 1 },
      active_count: 1,
      recent_count: 1,
      agents: [],
    },
  ],
  events: [],
};

describe("buildSidebarFlowRows", () => {
  it("merges editable catalog flows with runtime activity", () => {
    const rows = buildSidebarFlowRows([file], top);
    const demo = rows.find((row) => row.name === "demo");
    const runtimeOnly = rows.find((row) => row.name === "runtime-only");

    expect(demo?.path).toBe("/flows/demo.yaml");
    expect(demo?.description).toBe("Editable demo.");
    expect(demo?.activeCount).toBe(1);
    expect(demo?.latestMessage).toContain("github.com/example");
    expect(runtimeOnly?.path).toBe("");
    expect(runtimeOnly?.counts.needs_help).toBe(1);
  });
});
