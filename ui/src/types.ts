export type AgentStatus = "waiting" | "working" | "paused" | "needs_help" | "finished";

export interface AgentRow {
  id: number;
  status: AgentStatus;
  timer_seconds: number;
  args: Record<string, string>;
  display_args: Record<string, string>;
  state_name: string;
  substate: string;
  phase: string;
  cwd: string;
  ready_at: string;
  ended_at: string;
}

export interface StateRows {
  waiting: AgentRow[];
  working: AgentRow[];
  paused: AgentRow[];
  needs_help: AgentRow[];
  finished: AgentRow[];
}

export interface FocusStateVisit {
  id: number;
  started_at: string;
  ended_at: string;
  duration_seconds: number;
  duration_text: string;
}

export interface FocusStateSummary {
  state_name: string;
  count: number;
  latest_duration_seconds: number;
  latest_duration_text: string;
  total_duration_seconds: number;
  total_duration_text: string;
  visits: FocusStateVisit[];
}

export interface FocusEdgeItem {
  id: number;
  created_at: string;
  choice: string;
  reason: string;
  absolute_time_text: string;
}

export interface FocusEdgeSummary {
  key: string;
  count: number;
  latest_created_at: string;
  latest_time_text: string;
  latest_reason: string;
  items: FocusEdgeItem[];
}

export interface FlowState {
  id: string;
  name: string;
  start: boolean;
  end: boolean;
  synthetic: boolean;
  rows: StateRows;
  focus?: FocusStateSummary;
}

export interface FlowEdge {
  id: string;
  key: string;
  source: string;
  target: string;
  transition_labels: string[];
  transition_label_text: string;
  focus?: FocusEdgeSummary | null;
}

export interface RuntimeDiagnostic {
  kind: string;
  level: "error" | "warning";
  created_at: string;
  message: string;
  details?: string[];
  agent_id?: number;
  state_name?: string;
}

export interface RuntimeSummary {
  active: boolean;
  pid: number | null;
  started_at: string;
  heartbeat_at: string;
  uptime_seconds: number;
  diagnostics: RuntimeDiagnostic[];
}

export interface FlowCounts {
  waiting: number;
  working: number;
  paused: number;
  needs_help: number;
}

export interface FocusAgent {
  id: number;
  flow_name: string;
  current_state: string;
  substate: string;
  phase: string;
  status: AgentStatus;
  timer_seconds: number;
  status_message: string;
  cwd: string;
  args: Record<string, string>;
  display_args: Record<string, string>;
  ready_at: string;
  ended_at: string;
  created_at: string;
  state_options: string[];
}

export interface FocusEvent {
  id: number;
  kind: string;
  created_at: string;
  absolute_time_text: string;
  relative_time_text: string;
  text: string;
  link: { type: "state" | "edge"; key: string } | null;
}

export interface FocusSnapshot {
  agent: FocusAgent;
  events: FocusEvent[];
  states: Record<string, FocusStateSummary>;
  edges: Record<string, FocusEdgeSummary>;
}

export interface OverviewSnapshot {
  runtime: RuntimeSummary;
  flow: {
    name: string;
    counts: FlowCounts;
    states: FlowState[];
    edges: FlowEdge[];
  };
  focus?: FocusSnapshot;
}

export interface LaunchContext {
  flowName: string;
  apiBaseUrl: string;
}
