import { render, screen } from "@testing-library/react";

import { TopStrip } from "./TopStrip";
import type { OverviewSnapshot } from "../types";

function makeSnapshot(description: string): OverviewSnapshot {
  return {
    runtime: {
      active: true,
      pid: 42,
      started_at: "2026-04-02T10:00:00Z",
      heartbeat_at: "2026-04-02T10:00:01Z",
      uptime_seconds: 60,
      diagnostics: [],
    },
    flow: {
      name: "demo",
      description,
      counts: { waiting: 1, working: 1, paused: 0, needs_help: 0 },
      states: [],
      edges: [],
    },
  };
}

describe("TopStrip", () => {
  it("renders the flow description when present", () => {
    render(<TopStrip snapshot={makeSnapshot("Inspect CI and summarize failures.")} />);

    expect(screen.getByText("Inspect CI and summarize failures.")).toBeInTheDocument();
  });

  it("omits the description row when blank", () => {
    render(<TopStrip snapshot={makeSnapshot("  ")} />);

    expect(screen.queryByText(/Inspect CI/)).not.toBeInTheDocument();
  });
});
