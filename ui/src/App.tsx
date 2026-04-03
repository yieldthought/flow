import { Background, Controls, ReactFlow, ReactFlowProvider, type Edge, type EdgeTypes, type Node, type NodeTypes } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { startTransition, useEffect, useState } from "react";

import { fetchFocus, fetchOverview, postAction } from "./api";
import { buildGraphModel } from "./graph";
import { StateNode } from "./components/StateNode";
import { FlowEdge } from "./components/FlowEdge";
import { EventStrip } from "./components/EventStrip";
import { SelectedAgentHeader } from "./components/SelectedAgentHeader";
import { TopStrip } from "./components/TopStrip";
import { loadLaunchContext } from "./tauri";
import type { LaunchContext, OverviewSnapshot } from "./types";

const nodeTypes: NodeTypes = { state: StateNode };
const edgeTypes: EdgeTypes = { "flow-edge": FlowEdge };

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
    return <div className="app-loading">Loading Flow…</div>;
  }
  return (
    <ReactFlowProvider>
      <FlowApp flowName={context.flowName} apiBaseUrl={context.apiBaseUrl} />
    </ReactFlowProvider>
  );
}

export function FlowApp({ flowName, apiBaseUrl }: LaunchContext) {
  const [snapshot, setSnapshot] = useState<OverviewSnapshot | null>(null);
  const [selectedAgentId, setSelectedAgentId] = useState<number | null>(null);
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
          setSelectedAgentId(null);
        }
      }
    };

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

  if (error && !snapshot) {
    return <div className="app-error">{error}</div>;
  }
  if (!snapshot) {
    return <div className="app-loading">Loading {flowName}…</div>;
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
          setSelectedAgentId(agentId);
          setPinnedKey(null);
          setHoveredKey(null);
        });
      },
      onBackToOverview: () => {
        setSelectedAgentId(null);
        setPinnedKey(null);
        setHoveredKey(null);
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
    <div className="app-shell">
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
            onBack={() => setSelectedAgentId(null)}
          />
        ) : null}
        <div className="flow-canvas">
          <ReactFlow<Node, Edge>
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            fitView
            fitViewOptions={{ padding: 0.14 }}
            panOnScroll
            panOnDrag={false}
            minZoom={0.45}
            maxZoom={1.5}
            nodesDraggable={false}
            elementsSelectable={false}
            proOptions={{ hideAttribution: true }}
          >
            <Background color="rgba(104, 113, 134, 0.18)" size={1.25} gap={24} />
            <Controls position="bottom-left" showInteractive={false} />
          </ReactFlow>
        </div>
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
