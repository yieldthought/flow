import type { EditorDocument, EditorFilesResponse, EditorValidation, OverviewSnapshot, RuntimeTopSnapshot } from "./types";

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

export async function fetchRuntimeTop(apiBaseUrl: string, recent = "1h"): Promise<RuntimeTopSnapshot> {
  const response = await fetch(`${apiBaseUrl}/api/runtime/top?recent=${encodeURIComponent(recent)}`);
  if (!response.ok) {
    throw new Error(await errorText(response));
  }
  return (await response.json()) as RuntimeTopSnapshot;
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

export async function fetchEditorFiles(apiBaseUrl: string): Promise<EditorFilesResponse> {
  const response = await fetch(`${apiBaseUrl}/api/editor/files`);
  if (!response.ok) {
    throw new Error(await errorText(response));
  }
  return (await response.json()) as EditorFilesResponse;
}

export async function fetchEditorDocument(apiBaseUrl: string, path: string): Promise<EditorDocument> {
  const response = await fetch(`${apiBaseUrl}/api/editor/file?path=${encodeURIComponent(path)}`);
  if (!response.ok) {
    throw new Error(await errorText(response));
  }
  return (await response.json()) as EditorDocument;
}

export async function validateEditorDocument(
  apiBaseUrl: string,
  document: EditorDocument,
): Promise<EditorValidation> {
  const response = await fetch(`${apiBaseUrl}/api/editor/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ document }),
  });
  if (!response.ok) {
    throw new Error(await errorText(response));
  }
  return (await response.json()) as EditorValidation;
}

export async function saveEditorDocument(apiBaseUrl: string, document: EditorDocument): Promise<EditorDocument> {
  const response = await fetch(`${apiBaseUrl}/api/editor/file`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ document }),
  });
  if (!response.ok) {
    throw new Error(await errorText(response));
  }
  return (await response.json()) as EditorDocument;
}

async function errorText(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: string };
    return payload.detail ?? `${response.status} ${response.statusText}`;
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}
