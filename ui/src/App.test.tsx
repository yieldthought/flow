import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { loadLaunchContext } from "./tauri";

vi.mock("./tauri", () => ({
  loadLaunchContext: vi.fn(),
  openExternalUrl: vi.fn(),
}));

function jsonResponse(payload: unknown): Response {
  return {
    ok: true,
    json: async () => payload,
  } as Response;
}

describe("App", () => {
  beforeEach(() => {
    vi.mocked(loadLaunchContext).mockResolvedValue({ flowName: "", apiBaseUrl: "http://127.0.0.1:4123" });
    globalThis.fetch = vi.fn((url: RequestInfo | URL) => {
      const text = String(url);
      if (text.includes("/api/editor/files")) {
        return Promise.resolve(jsonResponse({ roots: [], files: [] }));
      }
      if (text.includes("/api/runtime/top")) {
        return Promise.resolve(jsonResponse({
          runtime: {
            active: true,
            pid: 123,
            started_at: "2026-05-04T10:00:00Z",
            heartbeat_at: "2026-05-04T10:00:01Z",
            uptime_seconds: 60,
            diagnostics: [],
          },
          summary: {
            active_agents: 0,
            recent_agents: 0,
            total_agents: 0,
            cumulative_agent_seconds: 0,
          },
          recent: { window: "1h", cutoff: "2026-05-04T09:00:00Z", seconds: 3600 },
          flows: [],
          events: [],
        }));
      }
      return Promise.resolve(jsonResponse({}));
    }) as typeof fetch;
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("opens the all-flow run overview when no launch flow is provided", async () => {
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Run Overview" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Overview/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run" })).toHaveClass("mode-switch__button--active");
  });
});
