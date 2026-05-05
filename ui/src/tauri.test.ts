import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const invokeMock = vi.fn();
const isTauriMock = vi.fn();

vi.mock("@tauri-apps/api/core", () => ({
  invoke: invokeMock,
  isTauri: isTauriMock,
}));

describe("loadLaunchContext", () => {
  const originalOpen = window.open;

  beforeEach(() => {
    invokeMock.mockReset();
    isTauriMock.mockReset();
    window.open = vi.fn();
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

  it("opens external links through Tauri when available", async () => {
    isTauriMock.mockReturnValue(true);
    invokeMock.mockResolvedValue(undefined);

    const { openExternalUrl } = await import("./tauri");

    await openExternalUrl("https://example.com/path");
    expect(invokeMock).toHaveBeenCalledWith("open_external_url", { url: "https://example.com/path" });
    expect(window.open).not.toHaveBeenCalled();
  });

  it("falls back to window.open outside Tauri", async () => {
    isTauriMock.mockReturnValue(false);

    const { openExternalUrl } = await import("./tauri");

    await openExternalUrl("https://example.com/path");
    expect(window.open).toHaveBeenCalledWith("https://example.com/path", "_blank", "noopener,noreferrer");
  });

  it("rejects non-http external links", async () => {
    const { openExternalUrl } = await import("./tauri");

    await expect(openExternalUrl("file:///tmp/demo")).rejects.toThrow("Only http and https");
  });

  afterEach(() => {
    window.open = originalOpen;
  });
});
