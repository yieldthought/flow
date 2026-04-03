import type { AgentStatus, FocusEdgeSummary, FocusEvent, FocusStateSummary, RuntimeDiagnostic } from "./types";

export function formatCountdown(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return `${pad2(hours)}:${pad2(minutes)}:${pad2(secs)}`;
}

export function formatCompactDuration(seconds: number): string {
  const totalMinutes = Math.max(0, Math.floor(seconds / 60));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return `${hours}h ${minutes}m`;
}

export function formatStatus(status: AgentStatus): string {
  if (status === "needs_help") {
    return "needs help";
  }
  return status;
}

export function formatArgs(args: Record<string, string>): string {
  const entries = Object.entries(args);
  if (entries.length === 0) {
    return "defaults";
  }
  return entries
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}=${value}`)
    .join(" ");
}

export function edgeSummaryText(summary: FocusEdgeSummary | null | undefined): string {
  if (!summary) {
    return "";
  }
  const reason = summary.latest_reason ? truncate(summary.latest_reason, 48) : "no reason";
  return `x${summary.count} ${summary.latest_time_text} ${reason}`;
}

export function edgeTransitionText(labels: string[] | undefined, fallback = ""): string {
  const filtered = (labels ?? []).filter((item) => item.trim().length > 0);
  if (filtered.length === 0) {
    return fallback;
  }
  return filtered.join(" / ");
}

export function stateSummaryText(summary: FocusStateSummary | null | undefined): string {
  if (!summary || summary.count === 0) {
    return "";
  }
  return `x${summary.count} latest ${summary.latest_duration_text} total ${summary.total_duration_text}`;
}

export function eventCardTitle(event: FocusEvent): string {
  return `${event.absolute_time_text} (${event.relative_time_text})`;
}

export function diagnosticText(item: RuntimeDiagnostic): string {
  if (item.kind === "daemon_crash") {
    return `${item.message}`;
  }
  if (item.agent_id !== undefined && item.state_name) {
    return `#${item.agent_id} ${item.state_name}: ${item.message}`;
  }
  return item.message;
}

export function truncate(value: string, maxLength: number): string {
  if (value.length <= maxLength) {
    return value;
  }
  return `${value.slice(0, Math.max(0, maxLength - 1))}…`;
}

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}
