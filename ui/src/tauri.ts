import type { LaunchContext } from "./types";

declare global {
  interface Window {
    __TAURI__?: unknown;
  }
}

export async function loadLaunchContext(): Promise<LaunchContext> {
  if (typeof window !== "undefined" && window.__TAURI__) {
    const { invoke } = await import("@tauri-apps/api/core");
    return (await invoke("launch_context")) as LaunchContext;
  }

  const params = new URLSearchParams(window.location.search);
  const flowName = params.get("flow") ?? import.meta.env.VITE_FLOW_UI_FLOW_NAME ?? "";
  const apiBaseUrl = params.get("api") ?? import.meta.env.VITE_FLOW_UI_API_BASE_URL ?? "";
  if (apiBaseUrl) {
    return { flowName, apiBaseUrl };
  }
  throw new Error("Missing launch context");
}
