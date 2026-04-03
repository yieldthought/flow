import dagre from "dagre";
import type { Edge, Node } from "@xyflow/react";

import type { FlowEdge, FlowState, OverviewSnapshot } from "./types";

export type GraphStateNodeData = Record<string, unknown> & {
  mode: "overview" | "focus";
  state: FlowState;
  currentStateName?: string;
  selectedAgentId?: number | null;
  hoveredKey?: string | null;
  pinnedKey?: string | null;
  dragAgentId?: number | null;
  onSelectAgent?: (agentId: number) => void;
  onBackToOverview?: () => void;
  onMoveAgent?: (stateName: string) => void;
  onPauseAgent?: () => void;
  onHoverKey?: (key: string | null) => void;
  onPinKey?: (key: string | null) => void;
  onDragAgent?: (agentId: number | null) => void;
};

export type GraphEdgeData = Record<string, unknown> & {
  mode: "overview" | "focus";
  edge: FlowEdge;
  detourY?: number;
  selfLoop?: {
    left: number;
    right: number;
    top: number;
    bottom: number;
  };
  hoveredKey?: string | null;
  pinnedKey?: string | null;
  onHoverKey?: (key: string | null) => void;
  onPinKey?: (key: string | null) => void;
};

const OVERVIEW_NODE_WIDTH = 288;
const FOCUS_NODE_WIDTH = 304;
const OVERVIEW_MIN_BODY_HEIGHT = 146;
const FOCUS_NODE_HEIGHT = 232;
const BODY_ROW_HEIGHT = 54;
const SHELF_ROW_HEIGHT = 46;
const SHELF_LABEL_HEIGHT = 18;
const SHELF_PADDING = 10;
const SECTION_GAP = 10;
const MAX_VISIBLE_ROWS = 3;
const COLUMN_GAP = 72;
type NodeLayout = {
  x: number;
  y: number;
  width: number;
  height: number;
  top: number;
  bottom: number;
  left: number;
  right: number;
};

export function buildGraphModel(snapshot: OverviewSnapshot): {
  mode: "overview" | "focus";
  nodes: Node<GraphStateNodeData>[];
  edges: Edge<GraphEdgeData>[];
} {
  const mode: "overview" | "focus" = snapshot.focus ? "focus" : "overview";
  const graph = new dagre.graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({
    rankdir: "LR",
    nodesep: 88,
    ranksep: 124,
    marginx: 48,
    marginy: 48,
  });

  const nodeLayouts = new Map<string, NodeLayout>();
  for (const state of snapshot.flow.states) {
    const width = mode === "focus" ? FOCUS_NODE_WIDTH : OVERVIEW_NODE_WIDTH;
    const height = mode === "focus" ? FOCUS_NODE_HEIGHT : overviewNodeHeight(state);
    graph.setNode(state.name, { width, height });
  }
  for (const edge of snapshot.flow.edges) {
    graph.setEdge(edge.source, edge.target);
  }
  dagre.layout(graph);
  populateLayouts(snapshot, graph, nodeLayouts, mode);
  alignLinearChains(snapshot.flow.edges, nodeLayouts);
  resolveColumnOverlaps(nodeLayouts);

  const nodes: Node<GraphStateNodeData>[] = snapshot.flow.states.map((state) => {
    const layout = nodeLayouts.get(state.name) ?? makeLayout(0, 0, mode === "focus" ? FOCUS_NODE_WIDTH : OVERVIEW_NODE_WIDTH, mode === "focus" ? FOCUS_NODE_HEIGHT : OVERVIEW_MIN_BODY_HEIGHT);
    return {
      id: state.name,
      type: "state",
      position: {
        x: layout.x - layout.width / 2,
        y: layout.y - layout.height / 2,
      },
      data: {
        mode,
        state: mode === "focus"
          ? {
              ...state,
              rows: {
                waiting: [],
                working: [],
                paused: [],
                needs_help: [],
                finished: [],
              },
            }
          : state,
        currentStateName: snapshot.focus?.agent.current_state,
      },
      draggable: false,
      selectable: false,
    };
  });

  const edges: Edge<GraphEdgeData>[] = snapshot.flow.edges.map((edge) => ({
    id: edge.key,
    source: edge.source,
    target: edge.target,
    type: "flow-edge",
    data: {
      mode,
      edge,
      detourY: computeDetourY(edge, nodeLayouts),
      selfLoop: computeSelfLoopBox(edge, nodeLayouts),
    },
    animated: false,
    selectable: false,
  }));

  return { mode, nodes, edges };
}

function overviewNodeHeight(state: FlowState): number {
  const bodyRows = clampRows(state.rows.working.length + state.rows.finished.length);
  const waitingHeight = shelfHeight(state.rows.waiting.length);
  const pausedHeight = shelfHeight(state.rows.paused.length);
  const needsHelpHeight = shelfHeight(state.rows.needs_help.length);
  const visibleShelves = [waitingHeight, pausedHeight, needsHelpHeight].filter((value) => value > 0);
  const bodyHeight = OVERVIEW_MIN_BODY_HEIGHT + bodyRows * BODY_ROW_HEIGHT;
  const gapCount = visibleShelves.length > 0 ? visibleShelves.length : 0;
  return bodyHeight + visibleShelves.reduce((total, value) => total + value, 0) + gapCount * SECTION_GAP;
}

function shelfHeight(count: number): number {
  if (count <= 0) {
    return 0;
  }
  return SHELF_LABEL_HEIGHT + SHELF_PADDING + clampRows(count) * SHELF_ROW_HEIGHT;
}

function clampRows(count: number): number {
  return Math.min(MAX_VISIBLE_ROWS, Math.max(0, count));
}

function populateLayouts(
  snapshot: OverviewSnapshot,
  graph: dagre.graphlib.Graph,
  nodeLayouts: Map<string, NodeLayout>,
  mode: "overview" | "focus",
): void {
  for (const state of snapshot.flow.states) {
    const fallbackWidth = mode === "focus" ? FOCUS_NODE_WIDTH : OVERVIEW_NODE_WIDTH;
    const fallbackHeight = mode === "focus" ? FOCUS_NODE_HEIGHT : overviewNodeHeight(state);
    const layout = graph.node(state.name) ?? { x: 0, y: 0, width: fallbackWidth, height: fallbackHeight };
    nodeLayouts.set(state.name, makeLayout(layout.x, layout.y, layout.width, layout.height));
  }
}

function makeLayout(x: number, y: number, width: number, height: number): NodeLayout {
  return {
    x,
    y,
    width,
    height,
    top: y - height / 2,
    bottom: y + height / 2,
    left: x - width / 2,
    right: x + width / 2,
  };
}

function updateLayoutBounds(layout: NodeLayout): void {
  layout.top = layout.y - layout.height / 2;
  layout.bottom = layout.y + layout.height / 2;
  layout.left = layout.x - layout.width / 2;
  layout.right = layout.x + layout.width / 2;
}

function alignLinearChains(edges: FlowEdge[], nodeLayouts: Map<string, NodeLayout>): void {
  const incoming = new Map<string, number>();
  const outgoing = new Map<string, number>();
  for (const edge of edges) {
    if (edge.source === edge.target) {
      continue;
    }
    outgoing.set(edge.source, (outgoing.get(edge.source) ?? 0) + 1);
    incoming.set(edge.target, (incoming.get(edge.target) ?? 0) + 1);
  }

  for (const edge of edges) {
    if (edge.source === edge.target) {
      continue;
    }
    if ((outgoing.get(edge.source) ?? 0) !== 1 || (incoming.get(edge.target) ?? 0) !== 1) {
      continue;
    }
    const source = nodeLayouts.get(edge.source);
    const target = nodeLayouts.get(edge.target);
    if (!source || !target) {
      continue;
    }
    target.y = source.y;
    updateLayoutBounds(target);
  }
}

function resolveColumnOverlaps(nodeLayouts: Map<string, NodeLayout>): void {
  const columns = new Map<number, NodeLayout[]>();
  for (const layout of nodeLayouts.values()) {
    const key = Math.round(layout.x);
    const bucket = columns.get(key) ?? [];
    bucket.push(layout);
    columns.set(key, bucket);
  }

  for (const bucket of columns.values()) {
    bucket.sort((left, right) => left.y - right.y);
    for (let index = 1; index < bucket.length; index += 1) {
      const previous = bucket[index - 1];
      const current = bucket[index];
      const minY = previous.bottom + COLUMN_GAP + current.height / 2;
      if (current.y < minY) {
        current.y = minY;
        updateLayoutBounds(current);
      }
    }
  }
}

function computeDetourY(
  edge: FlowEdge,
  nodeLayouts: Map<string, NodeLayout>,
): number | undefined {
  if (edge.source === edge.target) {
    return undefined;
  }
  const source = nodeLayouts.get(edge.source);
  const target = nodeLayouts.get(edge.target);
  if (!source || !target) {
    return undefined;
  }
  const left = Math.min(source.x, target.x);
  const right = Math.max(source.x, target.x);
  const corridorTop = Math.min(source.y, target.y) - 18;
  const corridorBottom = Math.max(source.y, target.y) + 18;
  const blockers = [...nodeLayouts.entries()]
    .filter(([name]) => name !== edge.source && name !== edge.target)
    .map(([, value]) => value)
    .filter((layout) => layout.x > left && layout.x < right)
    .filter((layout) => !(layout.bottom < corridorTop || layout.top > corridorBottom));

  if (blockers.length === 0) {
    return undefined;
  }
  const topY = Math.min(source.top, target.top, ...blockers.map((layout) => layout.top)) - 28;
  const bottomY = Math.max(source.bottom, target.bottom, ...blockers.map((layout) => layout.bottom)) + 28;
  const topCost = Math.abs(source.y - topY) + Math.abs(target.y - topY);
  const bottomCost = Math.abs(source.y - bottomY) + Math.abs(target.y - bottomY);
  return bottomCost <= topCost ? bottomY : topY;
}

function computeSelfLoopBox(
  edge: FlowEdge,
  nodeLayouts: Map<string, NodeLayout>,
): { left: number; right: number; top: number; bottom: number } | undefined {
  if (edge.source !== edge.target) {
    return undefined;
  }
  const layout = nodeLayouts.get(edge.source);
  if (!layout) {
    return undefined;
  }
  return {
    left: layout.left,
    right: layout.right,
    top: layout.top,
    bottom: layout.bottom,
  };
}
