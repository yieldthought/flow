import { beforeEach, describe, expect, it, vi } from "vitest";

const invokeMock = vi.fn();
const isTauriMock = vi.fn();

vi.mock("@tauri-apps/api/core", () => ({
  invoke: invokeMock,
  isTauri: isTauriMock,
}));

describe("loadLaunchContext", () => {
  beforeEach(() => {
    invokeMock.mockReset();
    isTauriMock.mockReset();
    window.history.replaceState({}, "", "/");
  });

  it("loads context through the Tauri command without requiring window.__TAURI__", async () => {
    isTauriMock.mockReturnValue(true);
    invokeMock.mockResolvedValue({ flowName: "demo", apiBaseUrl: "http://127.0.0.1:1234" });

    const { loadLaunchContext } = await import("./tauri");

    await expect(loadLaunchContext()).resolves.toEqual({
      flowName: "demo",
      apiBaseUrl: "http://127.0.0.1:1234",
    });
    expect(invokeMock).toHaveBeenCalledWith("launch_context");
  });

  it("falls back to query parameters outside Tauri", async () => {
    isTauriMock.mockReturnValue(false);
    window.history.replaceState({}, "", "/?flow=demo&api=http%3A%2F%2F127.0.0.1%3A5678");

    const { loadLaunchContext } = await import("./tauri");

    await expect(loadLaunchContext()).resolves.toEqual({
      flowName: "demo",
      apiBaseUrl: "http://127.0.0.1:5678",
    });
    expect(invokeMock).not.toHaveBeenCalled();
  });
});
