import type { OverviewSnapshot } from "./types";

export async function fetchOverview(apiBaseUrl: string, flowName: string): Promise<OverviewSnapshot> {
  const response = await fetch(`${apiBaseUrl}/api/flows/${encodeURIComponent(flowName)}`);
  if (!response.ok) {
    throw new Error(await errorText(response));
  }
  return (await response.json()) as OverviewSnapshot;
}

export async function fetchFocus(apiBaseUrl: string, flowName: string, agentId: number): Promise<OverviewSnapshot> {
  const response = await fetch(`${apiBaseUrl}/api/flows/${encodeURIComponent(flowName)}/agents/${agentId}`);
  if (!response.ok) {
    throw new Error(await errorText(response));
  }
  return (await response.json()) as OverviewSnapshot;
}

export async function postAction(
  apiBaseUrl: string,
  agentId: number,
  action: string,
  payload?: Record<string, unknown>,
): Promise<void> {
  const response = await fetch(`${apiBaseUrl}/api/agents/${agentId}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload ? JSON.stringify(payload) : undefined,
  });
  if (!response.ok) {
    throw new Error(await errorText(response));
  }
}

async function errorText(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: string };
    return payload.detail ?? `${response.status} ${response.statusText}`;
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}
