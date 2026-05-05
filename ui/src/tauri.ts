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

export async function openExternalUrl(url: string): Promise<void> {
  const target = new URL(url);
  if (target.protocol !== "http:" && target.protocol !== "https:") {
    throw new Error("Only http and https links can be opened");
  }

  try {
    const { invoke, isTauri } = await import("@tauri-apps/api/core");
    if (isTauri()) {
      await invoke("open_external_url", { url: target.toString() });
      return;
    }
  } catch {
    // Fall through to browser behavior outside Tauri or if the command is unavailable.
  }
  window.open(target.toString(), "_blank", "noopener,noreferrer");
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
