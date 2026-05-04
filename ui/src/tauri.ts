import type { LaunchContext } from "./types";

export async function loadLaunchContext(): Promise<LaunchContext> {
  const tauriContext = await loadTauriLaunchContext();
  if (tauriContext?.apiBaseUrl) {
    return tauriContext;
  }

  const params = new URLSearchParams(window.location.search);
  const flowName = params.get("flow") ?? import.meta.env.VITE_FLOW_UI_FLOW_NAME ?? "";
  const apiBaseUrl = params.get("api") ?? import.meta.env.VITE_FLOW_UI_API_BASE_URL ?? "";
  if (apiBaseUrl) {
    return { flowName, apiBaseUrl };
  }
  throw new Error("Missing launch context");
}

async function loadTauriLaunchContext(): Promise<LaunchContext | null> {
  try {
    const { invoke, isTauri } = await import("@tauri-apps/api/core");
    if (!isTauri()) {
      return null;
    }
    return await invoke<LaunchContext>("launch_context");
  } catch {
    return null;
  }
}
