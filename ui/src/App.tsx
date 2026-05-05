import {
  Background,
  Controls,
  PanOnScrollMode,
  ReactFlow,
  ReactFlowProvider,
  type Edge,
  type EdgeTypes,
  type Node,
  type NodeTypes,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { startTransition, useEffect, useMemo, useState } from "react";

import { fetchEditorFiles, fetchFocus, fetchOverview, fetchRuntimeTop, postAction } from "./api";
import { buildGraphModel } from "./graph";
import { StateNode, AgentHoverPopover } from "./components/StateNode";
import { FlowEdge } from "./components/FlowEdge";
import { EventStrip } from "./components/EventStrip";
import { SelectedAgentHeader } from "./components/SelectedAgentHeader";
import { TopStrip } from "./components/TopStrip";
import { LinkifiedText } from "./components/LinkifiedText";
import { VisualEditorApp } from "./EditorApp";
import { diagnosticText, formatArgs, formatCountdown, formatStatus } from "./format";
import { loadLaunchContext } from "./tauri";
import type {
  AgentStatus,
  EditorFileEntry,
  LaunchContext,
  OverviewSnapshot,
  RuntimeFlowSummary,
  RuntimeTopAgent,
  RuntimeTopEvent,
  RuntimeTopSnapshot,
} from "./types";

const nodeTypes: NodeTypes = { state: StateNode };
const edgeTypes: EdgeTypes = { "flow-edge": FlowEdge };

type ShellMode = "run" | "edit";

export interface SidebarFlowRow {
  name: string;
  description: string;
  path: string;
  valid: boolean;
  stateCount: number;
  transitionCount: number;
  activeCount: number;
  recentCount: number;
  counts: Record<AgentStatus, number>;
  latestMessage: string;
  file?: EditorFileEntry;
  runtime?: RuntimeFlowSummary;
}

export default function App() {
  const [context, setContext] = useState<LaunchContext | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    void loadLaunchContext()
      .then((value) => {
        if (!cancelled) {
          setContext(value);
        }
      })
      .catch((exc: Error) => {
        if (!cancelled) {
          setError(exc.message);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return <div className="app-error">{error}</div>;
  }
  if (!context) {
    return <div className="app-loading">Loading Flow...</div>;
  }
  return (
    <UnifiedFlowApp apiBaseUrl={context.apiBaseUrl} flowName={context.flowName} />
  );
}

function UnifiedFlowApp({ apiBaseUrl, flowName }: LaunchContext) {
  const [mode, setMode] = useState<ShellMode>("run");
  const [selectedFlowName, setSelectedFlowName] = useState(flowName);
  const [selectedAgentId, setSelectedAgentId] = useState<number | null>(null);
  const [files, setFiles] = useState<EditorFileEntry[]>([]);
  const [topSnapshot, setTopSnapshot] = useState<RuntimeTopSnapshot | null>(null);
  const [sidebarQuery, setSidebarQuery] = useState("");
  const [editorDirty, setEditorDirty] = useState(false);
  const [notice, setNotice] = useState("");
  const [sidebarError, setSidebarError] = useState("");
  const [topError, setTopError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function loadFiles() {
      try {
        const payload = await fetchEditorFiles(apiBaseUrl);
        if (!cancelled) {
          setFiles(payload.files);
          setSidebarError("");
        }
      } catch (exc) {
        if (!cancelled) {
          setSidebarError(exc instanceof Error ? exc.message : "Failed to load flow files");
        }
      }
    }
    void loadFiles();
    return () => {
      cancelled = true;
    };
  }, [apiBaseUrl]);

  useEffect(() => {
    let cancelled = false;
    let timer = 0;
    async function loadTop() {
      try {
        const payload = await fetchRuntimeTop(apiBaseUrl);
        if (!cancelled) {
          setTopSnapshot(payload);
          setTopError("");
        }
      } catch (exc) {
        if (!cancelled) {
          setTopError(exc instanceof Error ? exc.message : "Failed to load runtime overview");
        }
      }
    }
    void loadTop();
    timer = window.setInterval(() => {
      void loadTop();
    }, 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [apiBaseUrl]);

  const rows = useMemo(() => buildSidebarFlowRows(files, topSnapshot), [files, topSnapshot]);
  const selectedRow = rows.find((row) => row.name === selectedFlowName);
  const selectedFilePath = selectedRow?.path || files.find((file) => file.name === selectedFlowName)?.path || "";

  useEffect(() => {
    if (mode !== "edit" || selectedFlowName || rows.length === 0) {
      return;
    }
    const firstEditable = rows.find((row) => row.path);
    if (firstEditable) {
      setSelectedFlowName(firstEditable.name);
    }
  }, [mode, selectedFlowName, rows]);

  async function refreshSidebar() {
    try {
      const [nextFiles, nextTop] = await Promise.all([fetchEditorFiles(apiBaseUrl), fetchRuntimeTop(apiBaseUrl)]);
      setFiles(nextFiles.files);
      setTopSnapshot(nextTop);
      setSidebarError("");
      setTopError("");
    } catch (exc) {
      setSidebarError(exc instanceof Error ? exc.message : "Failed to refresh flow list");
    }
  }

  function selectMode(nextMode: ShellMode) {
    setNotice("");
    if (nextMode === "edit" && !selectedFilePath) {
      const firstEditable = selectedRow?.path ? selectedRow : rows.find((row) => row.path);
      if (firstEditable) {
        setSelectedFlowName(firstEditable.name);
      } else {
        setNotice("No editable flow files were found.");
      }
    }
    setMode(nextMode);
    if (nextMode === "run") {
      setSelectedAgentId(null);
    }
  }

  function selectFlow(row: SidebarFlowRow) {
    setNotice("");
    if (mode === "edit") {
      if (!row.path) {
        setNotice(`No editable file was found for '${row.name}'.`);
        return;
      }
      if (editorDirty && row.name !== selectedFlowName) {
        setNotice("Unsaved edits remain in the current file.");
        return;
      }
    }
    setSelectedFlowName(row.name);
    setSelectedAgentId(null);
  }

  function selectAgent(flow: string, agentId: number) {
    setMode("run");
    setSelectedFlowName(flow);
    setSelectedAgentId(agentId);
    setNotice("");
  }

  const main = mode === "edit" ? (
    <ReactFlowProvider key={`edit:${selectedFilePath || selectedFlowName || "none"}`}>
      <VisualEditorApp
        apiBaseUrl={apiBaseUrl}
        preferredFlowName={selectedFlowName}
        selectedFilePath={selectedFilePath}
        files={files}
        hideSidebar
        onDirtyChange={setEditorDirty}
        onFilesChange={setFiles}
      />
    </ReactFlowProvider>
  ) : selectedFlowName ? (
    <RuntimeFlowPanel
      apiBaseUrl={apiBaseUrl}
      flowName={selectedFlowName}
      selectedAgentId={selectedAgentId}
      onSelectedAgentIdChange={setSelectedAgentId}
    />
  ) : (
    <RuntimeTopPanel
      snapshot={topSnapshot}
      error={topError}
      onSelectFlow={(name) => {
        setMode("run");
        setSelectedFlowName(name);
        setSelectedAgentId(null);
      }}
      onSelectAgent={selectAgent}
    />
  );

  return (
    <div className="flow-shell">
      <FlowSidebar
        mode={mode}
        rows={rows}
        query={sidebarQuery}
        selectedFlowName={selectedFlowName}
        selectedOverview={mode === "run" && !selectedFlowName}
        onModeChange={selectMode}
        onQueryChange={setSidebarQuery}
        onSelectOverview={() => {
          setMode("run");
          setSelectedFlowName("");
          setSelectedAgentId(null);
        }}
        onSelectFlow={selectFlow}
        onRefresh={() => void refreshSidebar()}
      />
      <main className="flow-shell__main">
        {notice ? <div className="editor-notice flow-shell__notice">{notice}</div> : null}
        {sidebarError ? <div className="editor-error flow-shell__notice">{sidebarError}</div> : null}
        {main}
      </main>
    </div>
  );
}

function FlowSidebar({
  mode,
  rows,
  query,
  selectedFlowName,
  selectedOverview,
  onModeChange,
  onQueryChange,
  onSelectOverview,
  onSelectFlow,
  onRefresh,
}: {
  mode: ShellMode;
  rows: SidebarFlowRow[];
  query: string;
  selectedFlowName: string;
  selectedOverview: boolean;
  onModeChange: (mode: ShellMode) => void;
  onQueryChange: (query: string) => void;
  onSelectOverview: () => void;
  onSelectFlow: (row: SidebarFlowRow) => void;
  onRefresh: () => void;
}) {
  const filteredRows = rows.filter((row) => {
    const haystack = [
      row.name,
      row.description,
      row.path,
      row.activeCount ? "active running" : "",
      row.recentCount ? "recent history" : "",
      row.counts.needs_help ? "needs help" : "",
      row.latestMessage,
    ].join(" ").toLowerCase();
    return haystack.includes(query.trim().toLowerCase());
  });

  return (
    <aside className="flow-sidebar">
      <div className="editor-sidebar__header">
        <div>
          <div className="eyebrow">Flow</div>
          <div className="editor-sidebar__title">Runtime</div>
        </div>
        <button className="icon-button" type="button" title="Refresh flow list" aria-label="Refresh flow list" onClick={onRefresh}>
          ↻
        </button>
      </div>
      <div className="mode-switch" role="group" aria-label="Mode">
        <button className={mode === "run" ? "mode-switch__button mode-switch__button--active" : "mode-switch__button"} type="button" onClick={() => onModeChange("run")}>
          Run
        </button>
        <button className={mode === "edit" ? "mode-switch__button mode-switch__button--active" : "mode-switch__button"} type="button" onClick={() => onModeChange("edit")}>
          Edit
        </button>
      </div>
      <input
        className="editor-search"
        value={query}
        aria-label="Search flows"
        placeholder="Search"
        onChange={(event) => onQueryChange(event.target.value)}
      />
      <div className="editor-file-list nowheel">
        <button
          className={["editor-file", "flow-sidebar__overview", selectedOverview ? "editor-file--active" : ""].join(" ")}
          type="button"
          onClick={onSelectOverview}
        >
          <span className="editor-file__name">Overview</span>
          <span className="editor-file__meta">active agents across flows</span>
        </button>
        {filteredRows.map((row) => (
          <button
            className={[
              "editor-file",
              selectedFlowName === row.name ? "editor-file--active" : "",
              row.valid ? "" : "editor-file--invalid",
            ].join(" ")}
            type="button"
            data-testid="sidebar-flow-row"
            data-flow-name={row.name}
            key={row.name}
            onClick={() => onSelectFlow(row)}
          >
            <span className="editor-file__name">{row.name}</span>
            <span className="editor-file__meta">{sidebarMeta(row)}</span>
            {row.description ? <span className="editor-file__description">{row.description}</span> : null}
            <span className="flow-sidebar__badges">
              {row.activeCount ? <span className="summary-pill summary-pill--ok">{row.activeCount} active</span> : null}
              {row.recentCount && !row.activeCount ? <span className="summary-pill summary-pill--info">{row.recentCount} recent</span> : null}
              {row.counts.needs_help ? <span className="summary-pill summary-pill--error">{row.counts.needs_help} needs help</span> : null}
              {!row.path ? <span className="summary-pill">runtime only</span> : null}
            </span>
          </button>
        ))}
      </div>
    </aside>
  );
}

export function buildSidebarFlowRows(files: EditorFileEntry[], top: RuntimeTopSnapshot | null): SidebarFlowRow[] {
  const rows = new Map<string, SidebarFlowRow>();
  for (const file of files) {
    rows.set(file.name, {
      name: file.name,
      description: file.description,
      path: file.path,
      valid: file.valid,
      stateCount: file.stateCount,
      transitionCount: file.transitionCount,
      activeCount: 0,
      recentCount: 0,
      counts: { waiting: 0, working: 0, paused: 0, needs_help: 0, finished: 0 },
      latestMessage: "",
      file,
    });
  }
  for (const runtime of top?.flows ?? []) {
    const existing = rows.get(runtime.name);
    const latestAgent = [...runtime.agents].sort((left, right) => right.id - left.id)[0];
    rows.set(runtime.name, {
      name: runtime.name,
      description: existing?.description || runtime.description,
      path: existing?.path || "",
      valid: existing?.valid ?? true,
      stateCount: existing?.stateCount ?? 0,
      transitionCount: existing?.transitionCount ?? 0,
      activeCount: runtime.active_count,
      recentCount: runtime.recent_count,
      counts: { ...runtime.counts, finished: 0 },
      latestMessage: latestAgent?.latest_message ?? existing?.latestMessage ?? "",
      file: existing?.file,
      runtime,
    });
  }
  return [...rows.values()].sort((left, right) => {
    const leftActivity = left.activeCount * 1000 + left.recentCount;
    const rightActivity = right.activeCount * 1000 + right.recentCount;
    if (leftActivity !== rightActivity) {
      return rightActivity - leftActivity;
    }
    return left.name.localeCompare(right.name);
  });
}

function sidebarMeta(row: SidebarFlowRow): string {
  const fileMeta = row.path ? `${row.stateCount} states / ${row.transitionCount} decisions` : "no catalog file";
  if (row.activeCount) {
    return `${fileMeta} · ${row.activeCount} active`;
  }
  if (row.recentCount) {
    return `${fileMeta} · ${row.recentCount} recent`;
  }
  return fileMeta;
}

export function FlowApp({ flowName, apiBaseUrl }: LaunchContext) {
  const [selectedAgentId, setSelectedAgentId] = useState<number | null>(null);
  return (
    <div className="app-shell">
      <RuntimeFlowPanel
        apiBaseUrl={apiBaseUrl}
        flowName={flowName}
        selectedAgentId={selectedAgentId}
        onSelectedAgentIdChange={setSelectedAgentId}
      />
    </div>
  );
}

function RuntimeFlowPanel({
  flowName,
  apiBaseUrl,
  selectedAgentId,
  onSelectedAgentIdChange,
}: LaunchContext & {
  selectedAgentId: number | null;
  onSelectedAgentIdChange: (agentId: number | null) => void;
}) {
  const [snapshot, setSnapshot] = useState<OverviewSnapshot | null>(null);
  const [error, setError] = useState<string>("");
  const [busy, setBusy] = useState<string | null>(null);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [hoveredKey, setHoveredKey] = useState<string | null>(null);
  const [pinnedKey, setPinnedKey] = useState<string | null>(null);
  const [moveTarget, setMoveTarget] = useState("");
  const [dragAgentId, setDragAgentId] = useState<number | null>(null);
  const [toast, setToast] = useState<{
    message: string;
    undoAction?: { action: "resume" | "move"; payload?: Record<string, unknown> };
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer = 0;

    const load = async () => {
      try {
        const next = selectedAgentId === null
          ? await fetchOverview(apiBaseUrl, flowName)
          : await fetchFocus(apiBaseUrl, flowName, selectedAgentId);
        if (cancelled) {
          return;
        }
        setSnapshot(next);
        setError("");
      } catch (exc) {
        if (cancelled) {
          return;
        }
        setError(exc instanceof Error ? exc.message : "Failed to load flow UI");
        if (selectedAgentId !== null) {
          onSelectedAgentIdChange(null);
        }
      }
    };

    setSnapshot(null);
    void load();
    timer = window.setInterval(() => {
      void load();
    }, 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [apiBaseUrl, flowName, refreshNonce, selectedAgentId]);

  useEffect(() => {
    const agent = snapshot?.focus?.agent;
    if (!agent) {
      return;
    }
    const nextTarget = agent.state_options.find((name) => name !== agent.current_state) ?? "";
    setMoveTarget((current) => (current && current !== agent.current_state ? current : nextTarget));
  }, [snapshot?.focus?.agent]);

  async function runAction(action: "pause" | "interrupt" | "resume" | "wake" | "stop" | "move", payload?: Record<string, unknown>) {
    const agent = snapshot?.focus?.agent;
    if (!agent) {
      return;
    }
    setBusy(action);
    setToast(null);
    try {
      await postAction(apiBaseUrl, agent.id, action, payload);
      if (action === "pause") {
        setToast({ message: `Paused #${agent.id}`, undoAction: { action: "resume" } });
      } else if (action === "move" && typeof payload?.state === "string") {
        setToast({
          message: `Moved #${agent.id} to ${payload.state}`,
          undoAction: { action: "move", payload: { state: agent.current_state } },
        });
      } else {
        setToast({ message: `${action} queued for #${agent.id}` });
      }
      setRefreshNonce((value) => value + 1);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : `Failed to ${action} agent`);
    } finally {
      setBusy(null);
      setDragAgentId(null);
    }
  }

  async function undoToast() {
    const agent = snapshot?.focus?.agent;
    if (!toast?.undoAction || !agent) {
      return;
    }
    setBusy("undo");
    try {
      await postAction(apiBaseUrl, agent.id, toast.undoAction.action, toast.undoAction.payload);
      setToast(null);
      setRefreshNonce((value) => value + 1);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to undo action");
    } finally {
      setBusy(null);
    }
  }

  function backToOverview() {
    onSelectedAgentIdChange(null);
    setPinnedKey(null);
    setHoveredKey(null);
  }

  if (error && !snapshot) {
    return <div className="app-error">{error}</div>;
  }
  if (!snapshot) {
    return <div className="app-loading">Loading {flowName}...</div>;
  }

  const graph = buildGraphModel(snapshot);
  const nodes = graph.nodes.map((node) => ({
    ...node,
    data: {
      ...node.data,
      selectedAgentId,
      hoveredKey,
      pinnedKey,
      dragAgentId,
      onSelectAgent: (agentId: number) => {
        startTransition(() => {
          onSelectedAgentIdChange(agentId);
          setPinnedKey(null);
          setHoveredKey(null);
        });
      },
      onBackToOverview: () => {
        backToOverview();
      },
      onMoveAgent: (stateName: string) => {
        void runAction("move", { state: stateName });
      },
      onPauseAgent: () => {
        void runAction("pause");
      },
      onHoverKey: setHoveredKey,
      onPinKey: (key: string | null) => setPinnedKey((current) => (current === key ? null : key)),
      onDragAgent: setDragAgentId,
    },
  }));
  const edges = graph.edges.map((edge) => ({
    ...edge,
    data: {
      ...edge.data,
      hoveredKey,
      pinnedKey,
      onHoverKey: setHoveredKey,
      onPinKey: (key: string | null) => setPinnedKey((current) => (current === key ? null : key)),
    },
  }));

  return (
    <div className="runtime-flow-panel">
      <TopStrip snapshot={snapshot} />
      <main className="flow-panel">
        {snapshot.focus ? (
          <SelectedAgentHeader
            agent={snapshot.focus.agent}
            moveTarget={moveTarget}
            busy={busy}
            onMoveTargetChange={setMoveTarget}
            onAction={(action) => {
              if (action === "move") {
                void runAction("move", { state: moveTarget });
              } else {
                void runAction(action);
              }
            }}
            onDragStart={setDragAgentId}
            onDragEnd={() => setDragAgentId(null)}
            onBack={backToOverview}
          />
        ) : null}
        <ReactFlowProvider key={`runtime:${flowName}:${selectedAgentId ?? "overview"}`}>
          <div className="flow-canvas">
            <ReactFlow<Node, Edge>
              key={selectedAgentId === null ? `overview:${flowName}` : `focus:${selectedAgentId}`}
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              edgeTypes={edgeTypes}
              fitView
              fitViewOptions={{ padding: 0.14 }}
              panOnScroll
              panOnScrollMode={PanOnScrollMode.Free}
              panOnDrag
              zoomOnPinch
              zoomOnScroll={false}
              minZoom={0.18}
              maxZoom={1.8}
              nodesDraggable={false}
              elementsSelectable={false}
              onPaneClick={() => {
                if (selectedAgentId !== null) {
                  backToOverview();
                }
              }}
              proOptions={{ hideAttribution: true }}
            >
              <Background color="var(--canvas-grid)" size={1.2} gap={28} />
              <Controls position="bottom-left" showInteractive={false} />
            </ReactFlow>
          </div>
        </ReactFlowProvider>
        {snapshot.focus ? (
          <EventStrip
            events={snapshot.focus.events}
            hoveredKey={hoveredKey}
            pinnedKey={pinnedKey}
            onHoverKey={setHoveredKey}
            onPinKey={(key) => setPinnedKey((current) => (current === key ? null : key))}
          />
        ) : null}
      </main>
      {error ? <div className="error-banner">{error}</div> : null}
      {toast ? (
        <div className="toast">
          <span>{toast.message}</span>
          {toast.undoAction ? (
            <button type="button" onClick={() => void undoToast()}>
              undo
            </button>
          ) : (
            <button type="button" onClick={() => setToast(null)}>
              dismiss
            </button>
          )}
        </div>
      ) : null}
    </div>
  );
}

function RuntimeTopPanel({
  snapshot,
  error,
  onSelectFlow,
  onSelectAgent,
}: {
  snapshot: RuntimeTopSnapshot | null;
  error: string;
  onSelectFlow: (flowName: string) => void;
  onSelectAgent: (flowName: string, agentId: number) => void;
}) {
  const [hoveredAgent, setHoveredAgent] = useState<RuntimeTopAgent | null>(null);
  const latestEvents = [...(snapshot?.events ?? [])].reverse().slice(0, 120);
  const agentEntries = useMemo<RuntimeAgentEntry[]>(() => {
    if (!snapshot) {
      return [];
    }
    return snapshot.flows.flatMap((flow) => flow.agents.map((agent) => ({ flow, agent })));
  }, [snapshot]);
  const activeEntries = agentEntries.filter(({ agent }) => !agent.ended_at);
  const visibleAgentEntries = activeEntries.length ? activeEntries : agentEntries;

  if (error && !snapshot) {
    return <div className="app-error">{error}</div>;
  }
  if (!snapshot) {
    return <div className="app-loading">Loading runtime overview...</div>;
  }

  const activeCount = snapshot.summary?.active_agents ?? snapshot.flows.reduce((total, flow) => total + flow.active_count, 0);
  const recentCount = snapshot.summary?.recent_agents ?? snapshot.flows.reduce((total, flow) => total + flow.recent_count, 0);
  const totalCount = snapshot.summary?.total_agents ?? recentCount;
  const cumulativeSeconds = snapshot.summary?.cumulative_agent_seconds ?? 0;
  const banner = snapshot.runtime.diagnostics[0];
  const agentTitle = activeEntries.length ? "Active Agents" : "Recent Agents";

  return (
    <section className="runtime-top-pane">
      <header className="runtime-top-header">
        <div className="runtime-top-summary-line">
          <h1 className="runtime-top-summary-line__label">Run Overview</h1>
          <span className={`runtime-top-status runtime-top-status--${snapshot.runtime.active ? "ok" : "error"}`}>
            {snapshot.runtime.active ? "runtime active" : "runtime down"}
          </span>
          <span>uptime {formatCountdown(snapshot.runtime.uptime_seconds)}</span>
          <span>active agents {activeCount}</span>
          <span>recent agents {recentCount}</span>
          <span>total agents {totalCount}</span>
          <span>cumulative agent time {formatCountdown(cumulativeSeconds)}</span>
          <span>{snapshot.recent.window} recent window</span>
        </div>
        {banner ? (
          <div className={`diagnostic-banner diagnostic-banner--${banner.level}`}>
            <span className="diagnostic-banner__label">{banner.level}</span>
            <span>
              <LinkifiedText text={diagnosticText(banner)} />
            </span>
          </div>
        ) : null}
      </header>
      <div className="runtime-top-grid">
        <section className="runtime-top-section runtime-top-section--flows">
          <div className="runtime-top-section__title">
            <span>{agentTitle}</span>
            <span>{visibleAgentEntries.length} shown</span>
          </div>
          <div className="runtime-agent-ledger nowheel">
            {visibleAgentEntries.length ? visibleAgentEntries.map(({ flow, agent }) => (
              <RuntimeAgentRow
                agent={agent}
                flow={flow}
                key={`${flow.name}-${agent.id}`}
                onHover={setHoveredAgent}
                onSelectAgent={onSelectAgent}
                onSelectFlow={onSelectFlow}
              />
            )) : <div className="runtime-empty">No active or recent agents.</div>}
            {hoveredAgent ? <AgentHoverPopover row={hoveredAgent} /> : null}
          </div>
        </section>
        <section className="runtime-top-section runtime-top-section--events">
          <div className="runtime-top-section__title">
            <span>Recent History</span>
            <span>{latestEvents.length} events</span>
          </div>
          <div className="runtime-event-list nowheel">
            {latestEvents.length ? latestEvents.map((event) => (
              <RuntimeEventRow event={event} key={`${event.agent_id}-${event.id}`} onSelectAgent={onSelectAgent} />
            )) : <div className="runtime-empty">No recent events.</div>}
          </div>
        </section>
      </div>
    </section>
  );
}

type RuntimeAgentEntry = {
  flow: RuntimeFlowSummary;
  agent: RuntimeTopAgent;
};

function RuntimeAgentRow({
  flow,
  agent,
  onHover,
  onSelectFlow,
  onSelectAgent,
}: {
  flow: RuntimeFlowSummary;
  agent: RuntimeTopAgent;
  onHover: (agent: RuntimeTopAgent | null) => void;
  onSelectFlow: (flowName: string) => void;
  onSelectAgent: (flowName: string, agentId: number) => void;
}) {
  const displayArgs = formatArgs(agent.display_args);
  const allArgs = formatArgs(agent.args);
  const argsText = displayArgs === "defaults" ? allArgs : displayArgs;
  const context = `${flow.name}${agent.state_name ? ` / ${agent.state_name}` : ""}`;

  return (
    <article
      className={`runtime-agent-row runtime-agent-row--${pillTone(agent.status)}`}
      onMouseEnter={() => onHover(agent)}
      onMouseLeave={() => onHover(null)}
    >
      <button className="runtime-agent-row__id" type="button" onClick={() => onSelectAgent(flow.name, agent.id)}>
        #{agent.id}
      </button>
      <span className="runtime-agent-row__status">{formatStatus(agent.status)}</span>
      <span className="runtime-agent-row__timer">{formatCountdown(agent.timer_seconds)}</span>
      <button className="runtime-agent-row__context" type="button" title={context} onClick={() => onSelectFlow(flow.name)}>
        {context}
      </button>
      <span className="runtime-agent-row__cwd" title={agent.cwd}>{agent.cwd}</span>
      <span className="runtime-agent-row__args" title={argsText}>
        <LinkifiedText text={argsText} />
      </span>
    </article>
  );
}

function RuntimeEventRow({
  event,
  onSelectAgent,
}: {
  event: RuntimeTopEvent;
  onSelectAgent: (flowName: string, agentId: number) => void;
}) {
  return (
    <article className="runtime-event-row">
      <span className="runtime-event-row__time">{event.absolute_time_text}</span>
      <button className="runtime-event-row__id" type="button" onClick={() => onSelectAgent(event.flow_name, event.agent_id)}>
        #{event.agent_id}
      </button>
      <button className="runtime-event-row__context" type="button" onClick={() => onSelectAgent(event.flow_name, event.agent_id)}>
        {event.flow_name}{event.state_name ? ` / ${event.state_name}` : ""}
      </button>
      <span className="runtime-event-row__text">
        <LinkifiedText text={event.text} />
      </span>
    </article>
  );
}

function pillTone(status: AgentStatus): string {
  if (status === "needs_help") {
    return "needs-help";
  }
  return status;
}
