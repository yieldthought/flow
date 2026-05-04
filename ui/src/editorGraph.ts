import dagre from "dagre";
import type { Edge, Node } from "@xyflow/react";

import { stateHue } from "./color";
import type { EditorDocument, EditorState, EditorTransition } from "./types";

export type EditorSelection =
  | { type: "flow" }
  | { type: "state"; stateId: string }
  | { type: "transition"; stateId: string; transitionId: string };

export type EditorStateNodeData = Record<string, unknown> & {
  state: EditorState;
  selected: boolean;
  selectedTransitionId: string;
  issueCount: number;
  onSelect: (selection: EditorSelection) => void;
  onUpdateState: (stateId: string, patch: Partial<EditorState>) => void;
  onAddTransition: (stateId: string) => void;
  onDeleteState: (stateId: string) => void;
};

export type EditorMissingNodeData = Record<string, unknown> & {
  name: string;
};

export type EditorEdgeData = Record<string, unknown> & {
  stateId: string;
  sourceName: string;
  transition: EditorTransition;
  transitionIndex: number;
  targetHue: number;
  route?: EditorEdgeRoute;
  label?: EditorEdgeLabel;
  selected: boolean;
  staleTarget: boolean;
  onSelect: (selection: EditorSelection) => void;
};

export type EditorEdgeLabel = {
  x: number;
  y: number;
  fraction: number;
  offset: number;
};

export type EditorEdgeRoute =
  | {
      kind: "perimeter";
      laneY: number;
      outboundX: number;
      inboundX: number;
      labelX: number;
      labelY: number;
    };

type LayoutNode = {
  id: string;
  width: number;
  height: number;
  x: number;
  y: number;
};

type Point = {
  x: number;
  y: number;
};

type Segment = {
  edgeIndex: number;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  dx: number;
  dy: number;
  length: number;
};

type EdgeGuide = {
  index: number;
  kind: "direct" | "perimeter";
  segments: Segment[];
  length: number;
};

type Rect = {
  left: number;
  right: number;
  top: number;
  bottom: number;
};

type EdgeDraft = {
  id: string;
  source: string;
  target: string;
  state: EditorState;
  targetState?: EditorState;
  transition: EditorTransition;
  transitionIndex: number;
  sourceHandle: string;
  route?: EditorEdgeRoute;
};

const STATE_WIDTH = 480;
const STATE_MIN_HEIGHT = 290;
const MISSING_WIDTH = 238;
const MISSING_HEIGHT = 118;
export const VISIBLE_DECISION_HANDLES = 4;
const DECISION_FIRST_CENTER_Y = 214;
const DECISION_ROW_PITCH = 42;

export function editorTransitionHandleId(transitionId: string): string {
  return `decision:${transitionId}`;
}

export function editorOverflowHandleId(stateId: string): string {
  return `decision-overflow:${stateId}`;
}

export function editorStateHue(name: string): number {
  return stateHue(name);
}

export function buildEditorGraphModel({
  document,
  selection,
  onSelect,
  onUpdateState,
  onAddTransition,
  onDeleteState,
}: {
  document: EditorDocument;
  selection: EditorSelection;
  onSelect: (selection: EditorSelection) => void;
  onUpdateState: (stateId: string, patch: Partial<EditorState>) => void;
  onAddTransition: (stateId: string) => void;
  onDeleteState: (stateId: string) => void;
}): {
  nodes: Node<EditorStateNodeData | EditorMissingNodeData>[];
  edges: Edge<EditorEdgeData>[];
} {
  const graph = new dagre.graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({
    rankdir: "LR",
    nodesep: 132,
    ranksep: 230,
    marginx: 72,
    marginy: 72,
  });

  const stateByName = new Map<string, EditorState>();
  const missingTargets = new Set<string>();
  const layouts = new Map<string, LayoutNode>();

  for (const state of document.states) {
    stateByName.set(state.name, state);
    graph.setNode(state.id, { width: STATE_WIDTH, height: stateHeight(state) });
  }

  for (const state of document.states) {
    for (const transition of state.transitions) {
      if (!stateByName.has(transition.target)) {
        missingTargets.add(transition.target);
      }
    }
  }

  for (const target of missingTargets) {
    graph.setNode(missingNodeId(target), { width: MISSING_WIDTH, height: MISSING_HEIGHT });
  }

  const layoutEdges = new Set<string>();
  for (const state of document.states) {
    for (const transition of state.transitions) {
      const targetState = stateByName.get(transition.target);
      const targetId = targetState?.id ?? missingNodeId(transition.target);
      const key = `${state.id}->${targetId}`;
      if (!layoutEdges.has(key)) {
        layoutEdges.add(key);
        graph.setEdge(state.id, targetId);
      }
    }
  }

  dagre.layout(graph);

  for (const nodeId of graph.nodes()) {
    const node = graph.node(nodeId);
    layouts.set(nodeId, {
      id: nodeId,
      width: node.width,
      height: node.height,
      x: node.x,
      y: node.y,
    });
  }

  const stateIssues = issueCounts(document);
  const nodes: Node<EditorStateNodeData | EditorMissingNodeData>[] = document.states.map((state) => {
    const layout = layouts.get(state.id) ?? { id: state.id, width: STATE_WIDTH, height: stateHeight(state), x: 0, y: 0 };
    return {
      id: state.id,
      type: "editor-state",
      position: { x: layout.x - layout.width / 2, y: layout.y - layout.height / 2 },
      data: {
        state,
        selected: selection.type !== "flow" && selection.stateId === state.id,
        selectedTransitionId: selection.type === "transition" && selection.stateId === state.id
          ? selection.transitionId
          : "",
        issueCount: stateIssues.get(state.name) ?? 0,
        onSelect,
        onUpdateState,
        onAddTransition,
        onDeleteState,
      },
      draggable: false,
      selectable: true,
    };
  });

  for (const target of missingTargets) {
    const id = missingNodeId(target);
    const layout = layouts.get(id) ?? { id, width: MISSING_WIDTH, height: MISSING_HEIGHT, x: 0, y: 0 };
    nodes.push({
      id,
      type: "editor-missing",
      position: { x: layout.x - layout.width / 2, y: layout.y - layout.height / 2 },
      data: { name: target || "missing target" },
      draggable: false,
      selectable: false,
    });
  }

  const graphBounds = layoutBounds(layouts);
  const perimeterLanes = { top: 0, bottom: 0 };
  const edgeDrafts: EdgeDraft[] = [];
  for (const state of document.states) {
    state.transitions.forEach((transition, transitionIndex) => {
      const targetState = stateByName.get(transition.target);
      const targetId = targetState?.id ?? missingNodeId(transition.target);
      const sourceLayout = layouts.get(state.id);
      const targetLayout = layouts.get(targetId);
      edgeDrafts.push({
        id: `${state.id}-${transition.id}`,
        source: state.id,
        target: targetId,
        state,
        targetState,
        transition,
        transitionIndex: transitionIndex + 1,
        sourceHandle: transitionSourceHandle(state, transition, transitionIndex),
        route: sourceLayout && targetLayout
          ? feedbackRoute(sourceLayout, targetLayout, graphBounds, perimeterLanes)
          : undefined,
      });
    });
  }

  const edgeGuides = edgeDrafts.map((draft, index) => edgeGuide(draft, layouts, index));
  const nodeRects = [...layouts.values()].map((layout) => layoutRect(layout, 16));
  const placedLabelRects: Rect[] = [];
  const edges: Edge<EditorEdgeData>[] = edgeDrafts.map((draft, index) => {
    const label = chooseLabel(edgeGuides[index], edgeGuides, nodeRects, draft.transitionIndex, placedLabelRects);
    if (label) {
      placedLabelRects.push(centeredRect(label, 126, 30));
    }
    return {
      id: draft.id,
      source: draft.source,
      sourceHandle: draft.sourceHandle,
      target: draft.target,
      type: "editor-edge",
      data: {
        stateId: draft.state.id,
        sourceName: draft.state.name,
        transition: draft.transition,
        transitionIndex: draft.transitionIndex,
        targetHue: editorStateHue(draft.transition.target),
        route: draft.route,
        label,
        selected: selection.type === "transition" && selection.stateId === draft.state.id && selection.transitionId === draft.transition.id,
        staleTarget: !draft.targetState,
        onSelect,
      },
      selectable: false,
      animated: false,
    };
  });

  return { nodes, edges };
}

function feedbackRoute(
  source: LayoutNode,
  target: LayoutNode,
  bounds: { top: number; bottom: number; left: number; right: number },
  lanes: { top: number; bottom: number },
): EditorEdgeRoute | undefined {
  if (target.x > source.x) {
    return undefined;
  }

  const averageY = (source.y + target.y) / 2;
  const useTop = Math.abs(averageY - bounds.top) <= Math.abs(bounds.bottom - averageY);
  const laneIndex = useTop ? lanes.top++ : lanes.bottom++;
  const laneGap = 58;
  const laneY = useTop
    ? bounds.top - 120 - laneIndex * laneGap
    : bounds.bottom + 120 + laneIndex * laneGap;
  const sideGap = 110 + laneIndex * 24;
  const outboundX = Math.max(source.x + source.width / 2, target.x + target.width / 2) + sideGap;
  const inboundX = Math.min(source.x - source.width / 2, target.x - target.width / 2) - sideGap;

  return {
    kind: "perimeter",
    laneY,
    outboundX,
    inboundX,
    labelX: (outboundX + inboundX) / 2,
    labelY: laneY,
  };
}

function layoutBounds(layouts: Map<string, LayoutNode>): { top: number; bottom: number; left: number; right: number } {
  const values = [...layouts.values()];
  if (!values.length) {
    return { top: 0, bottom: 0, left: 0, right: 0 };
  }
  return {
    top: Math.min(...values.map((layout) => layout.y - layout.height / 2)),
    bottom: Math.max(...values.map((layout) => layout.y + layout.height / 2)),
    left: Math.min(...values.map((layout) => layout.x - layout.width / 2)),
    right: Math.max(...values.map((layout) => layout.x + layout.width / 2)),
  };
}

function edgeGuide(draft: EdgeDraft, layouts: Map<string, LayoutNode>, edgeIndex: number): EdgeGuide {
  const source = layouts.get(draft.source);
  const target = layouts.get(draft.target);
  if (!source || !target) {
    return { index: edgeIndex, kind: "direct", segments: [], length: 0 };
  }

  const sourcePoint = {
    x: source.x + source.width / 2,
    y: sourceDecisionY(draft, source),
  };
  const targetPoint = {
    x: target.x - target.width / 2,
    y: target.y,
  };

  const points = draft.route?.kind === "perimeter"
    ? [
        sourcePoint,
        { x: draft.route.outboundX, y: sourcePoint.y },
        { x: draft.route.outboundX, y: draft.route.laneY },
        { x: draft.route.inboundX, y: draft.route.laneY },
        { x: draft.route.inboundX, y: targetPoint.y },
        targetPoint,
      ]
    : bezierGuidePoints(sourcePoint, targetPoint);

  const segments = pointsToSegments(points, edgeIndex);
  return {
    index: edgeIndex,
    kind: draft.route?.kind === "perimeter" ? "perimeter" : "direct",
    segments,
    length: segments.reduce((sum, segment) => sum + segment.length, 0),
  };
}

function chooseLabel(
  guide: EdgeGuide | undefined,
  allGuides: EdgeGuide[],
  nodeRects: Rect[],
  transitionIndex: number,
  placedLabelRects: Rect[],
): EditorEdgeLabel | undefined {
  if (!guide || !guide.segments.length || guide.length <= 0) {
    return undefined;
  }

  const preferredFraction = guide.kind === "perimeter" ? 0.52 : 0.68;
  const fractions = guide.kind === "perimeter"
    ? [0.5, 0.42, 0.58, 0.32, 0.68, 0.2, 0.8, 0.1, 0.9]
    : [0.68, 0.78, 0.56, 0.38, 0.86, 0.22, 0.94, 0.1];
  const firstSide = (guide.index + transitionIndex) % 2 === 0 ? 1 : -1;
  const otherSegments = allGuides.filter((item) => item.index !== guide.index).flatMap((item) => item.segments);
  const labelWidth = 126;
  const labelHeight = 30;
  let best: { label: EditorEdgeLabel; score: number } | undefined;

  for (const fraction of fractions) {
    const sample = sampleGuide(guide, fraction);
    if (!sample) {
      continue;
    }
    const normalLength = Math.hypot(sample.segment.dy, sample.segment.dx) || 1;
    const normal = {
      x: -sample.segment.dy / normalLength,
      y: sample.segment.dx / normalLength,
    };

    const offsets = [0, firstSide * 16, -firstSide * 16, firstSide * 28, -firstSide * 28];
    for (const offset of offsets) {
      const label = {
        x: sample.x + normal.x * offset,
        y: sample.y + normal.y * offset,
        fraction,
        offset,
      };
      const rect = centeredRect(label, labelWidth, labelHeight);
      const ownIntersects = guide.segments.some((segment) => segmentIntersectsRect(segment, rect));
      const ownDistance = distanceToSegments(label, guide.segments);
      let score = Math.abs(fraction - preferredFraction) * 8 + Math.abs(offset) * 0.18;
      score += ownIntersects ? -20 : ownDistance * 2.4;

      for (const segment of otherSegments) {
        if (segmentIntersectsRect(segment, rect)) {
          score += ownIntersects ? 260 : 520;
          continue;
        }
        const distance = distancePointToSegment(label, segment);
        if (distance < 54) {
          score += (54 - distance) * (ownIntersects ? 2.2 : 4.4);
        }
      }

      for (const nodeRect of nodeRects) {
        if (rectsOverlap(rect, nodeRect)) {
          score += 900;
        }
      }

      for (const placedRect of placedLabelRects) {
        if (rectsOverlap(rect, placedRect)) {
          score += 260;
        }
      }

      if (!best || score < best.score) {
        best = { label, score };
      }
    }
  }

  return best?.label;
}

function bezierGuidePoints(source: Point, target: Point): Point[] {
  const curvature = 0.34;
  const sourceControl = {
    x: source.x + calculateControlOffset(target.x - source.x, curvature),
    y: source.y,
  };
  const targetControl = {
    x: target.x - calculateControlOffset(target.x - source.x, curvature),
    y: target.y,
  };
  const points: Point[] = [];
  for (let index = 0; index <= 32; index += 1) {
    points.push(cubicBezierPoint(source, sourceControl, targetControl, target, index / 32));
  }
  return points;
}

function cubicBezierPoint(start: Point, controlA: Point, controlB: Point, end: Point, t: number): Point {
  const oneMinus = 1 - t;
  const a = oneMinus * oneMinus * oneMinus;
  const b = 3 * oneMinus * oneMinus * t;
  const c = 3 * oneMinus * t * t;
  const d = t * t * t;
  return {
    x: start.x * a + controlA.x * b + controlB.x * c + end.x * d,
    y: start.y * a + controlA.y * b + controlB.y * c + end.y * d,
  };
}

function calculateControlOffset(distance: number, curvature: number): number {
  if (distance >= 0) {
    return 0.5 * distance;
  }
  return curvature * 25 * Math.sqrt(-distance);
}

function pointsToSegments(points: Point[], edgeIndex: number): Segment[] {
  return points.slice(0, -1).flatMap((point, index) => {
    const next = points[index + 1];
    const dx = next.x - point.x;
    const dy = next.y - point.y;
    const length = Math.hypot(dx, dy);
    return length > 0
      ? [{ edgeIndex, x1: point.x, y1: point.y, x2: next.x, y2: next.y, dx, dy, length }]
      : [];
  });
}

function sampleGuide(guide: EdgeGuide, fraction: number): { x: number; y: number; segment: Segment } | undefined {
  const targetLength = guide.length * fraction;
  let traversed = 0;
  for (const segment of guide.segments) {
    if (traversed + segment.length >= targetLength) {
      const local = segment.length === 0 ? 0 : (targetLength - traversed) / segment.length;
      return {
        x: segment.x1 + segment.dx * local,
        y: segment.y1 + segment.dy * local,
        segment,
      };
    }
    traversed += segment.length;
  }
  const last = guide.segments[guide.segments.length - 1];
  return last ? { x: last.x2, y: last.y2, segment: last } : undefined;
}

function layoutRect(layout: LayoutNode, padding: number): Rect {
  return {
    left: layout.x - layout.width / 2 - padding,
    right: layout.x + layout.width / 2 + padding,
    top: layout.y - layout.height / 2 - padding,
    bottom: layout.y + layout.height / 2 + padding,
  };
}

function centeredRect(point: Point, width: number, height: number): Rect {
  return {
    left: point.x - width / 2,
    right: point.x + width / 2,
    top: point.y - height / 2,
    bottom: point.y + height / 2,
  };
}

function rectsOverlap(a: Rect, b: Rect): boolean {
  return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
}

function segmentIntersectsRect(segment: Segment, rect: Rect): boolean {
  const start = { x: segment.x1, y: segment.y1 };
  const end = { x: segment.x2, y: segment.y2 };
  if (pointInRect(start, rect) || pointInRect(end, rect)) {
    return true;
  }
  const corners = [
    { x: rect.left, y: rect.top },
    { x: rect.right, y: rect.top },
    { x: rect.right, y: rect.bottom },
    { x: rect.left, y: rect.bottom },
  ];
  return corners.some((corner, index) => {
    const next = corners[(index + 1) % corners.length];
    return segmentsIntersect(start, end, corner, next);
  });
}

function pointInRect(point: Point, rect: Rect): boolean {
  return point.x >= rect.left && point.x <= rect.right && point.y >= rect.top && point.y <= rect.bottom;
}

function segmentsIntersect(a: Point, b: Point, c: Point, d: Point): boolean {
  const det = (b.x - a.x) * (d.y - c.y) - (b.y - a.y) * (d.x - c.x);
  if (Math.abs(det) < 0.0001) {
    return false;
  }
  const t = ((c.x - a.x) * (d.y - c.y) - (c.y - a.y) * (d.x - c.x)) / det;
  const u = ((c.x - a.x) * (b.y - a.y) - (c.y - a.y) * (b.x - a.x)) / det;
  return t >= 0 && t <= 1 && u >= 0 && u <= 1;
}

function distancePointToSegment(point: Point, segment: Segment): number {
  const lengthSquared = segment.length * segment.length;
  if (lengthSquared === 0) {
    return Math.hypot(point.x - segment.x1, point.y - segment.y1);
  }
  const raw = ((point.x - segment.x1) * segment.dx + (point.y - segment.y1) * segment.dy) / lengthSquared;
  const clamped = Math.max(0, Math.min(1, raw));
  const x = segment.x1 + segment.dx * clamped;
  const y = segment.y1 + segment.dy * clamped;
  return Math.hypot(point.x - x, point.y - y);
}

function distanceToSegments(point: Point, segments: Segment[]): number {
  if (!segments.length) {
    return 0;
  }
  return Math.min(...segments.map((segment) => distancePointToSegment(point, segment)));
}

function stateHeight(state: EditorState): number {
  const promptRows = Math.min(8, Math.max(3, Math.ceil(state.prompt.length / 92)));
  const transitionRows = state.transitions.length > VISIBLE_DECISION_HANDLES
    ? VISIBLE_DECISION_HANDLES + 1
    : Math.max(1, state.transitions.length);
  return Math.max(STATE_MIN_HEIGHT, 178 + promptRows * 20 + transitionRows * 42);
}

function missingNodeId(target: string): string {
  return `missing:${target || "empty"}`;
}

function transitionSourceHandle(state: EditorState, transition: EditorTransition, transitionIndex: number): string {
  if (transitionIndex < VISIBLE_DECISION_HANDLES) {
    return editorTransitionHandleId(transition.id);
  }
  return editorOverflowHandleId(state.id);
}

function sourceDecisionY(draft: EdgeDraft, source: LayoutNode): number {
  const rowIndex = Math.min(draft.transitionIndex - 1, VISIBLE_DECISION_HANDLES);
  return source.y - source.height / 2 + DECISION_FIRST_CENTER_Y + rowIndex * DECISION_ROW_PITCH;
}

function issueCounts(document: EditorDocument): Map<string, number> {
  const counts = new Map<string, number>();
  for (const message of [...document.validation.errors, ...document.validation.warnings]) {
    for (const state of document.states) {
      if (message.includes(`'${state.name}'`) || message.includes(` ${state.name} `)) {
        counts.set(state.name, (counts.get(state.name) ?? 0) + 1);
      }
    }
  }
  return counts;
}
