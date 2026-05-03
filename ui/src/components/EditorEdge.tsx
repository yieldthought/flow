import type { CSSProperties } from "react";
import { BaseEdge, EdgeLabelRenderer, Position, getBezierPath, type Edge, type EdgeProps } from "@xyflow/react";

import type { EditorEdgeData } from "../editorGraph";

type DirectionMarker = {
  x: number;
  y: number;
  angle: number;
};

type Point = {
  x: number;
  y: number;
};

type Segment = {
  x1: number;
  y1: number;
  dx: number;
  dy: number;
  length: number;
};

type RenderedRoute = {
  path: string;
  labelX: number;
  labelY: number;
  markers: DirectionMarker[];
  segments: Segment[];
  length: number;
};

type EdgeHueStyle = CSSProperties & {
  "--edge-hue"?: number;
};

export function EditorEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
}: EdgeProps<Edge<EditorEdgeData>>) {
  const typed = data as EditorEdgeData | undefined;
  const edgeHue = typed?.targetHue ?? 210;
  const route = typed?.route?.kind === "perimeter"
    ? perimeterPath({
        sourceX,
        sourceY,
        targetX,
        targetY,
        laneY: typed.route.laneY,
        outboundX: typed.route.outboundX,
        inboundX: typed.route.inboundX,
        labelX: typed.route.labelX,
        labelY: typed.route.labelY,
      })
    : bezierPath({
        sourceX,
        sourceY,
        sourcePosition,
        targetX,
        targetY,
        targetPosition,
      });
  const transition = typed?.transition;
  const condition = transition?.condition ?? "";
  const target = transition?.target ?? "";
  const visibleLabel = transition?.wait ? `wait ${transition.wait}` : `d${typed?.transitionIndex ?? 1}`;
  const accessibleLabel = transitionLabel(condition, target, typed?.transitionIndex ?? 1);
  const label = routeLabel(route, typed?.label);

  return (
    <>
      <BaseEdge
        id={id}
        path={route.path}
        className={[
          "editor-edge__path",
          typed?.selected ? "editor-edge__path--selected" : "",
          typed?.staleTarget ? "editor-edge__path--stale" : "",
        ].join(" ")}
        style={{
          "--edge-hue": edgeHue,
          strokeWidth: typed?.selected ? 2.6 : typed?.route?.kind === "perimeter" ? 1.8 : 1.5,
        } as EdgeHueStyle}
      />
      <EdgeLabelRenderer>
        {route.markers.map((marker, index) => (
          <div
            className={[
              "editor-edge-marker",
              typed?.selected ? "editor-edge-marker--selected" : "",
              typed?.staleTarget ? "editor-edge-marker--stale" : "",
            ].join(" ")}
            key={`${id}-marker-${index}`}
            style={{
              "--edge-hue": edgeHue,
              transform: `translate(-50%, -50%) translate(${marker.x}px, ${marker.y}px) rotate(${marker.angle}deg)`,
            } as EdgeHueStyle}
            aria-hidden="true"
          >
            <svg className="editor-edge-marker__triangle" viewBox="0 0 10 10">
              <path d="M 1 1 L 9 5 L 1 9 z" />
            </svg>
          </div>
        ))}
        <button
          className={[
            "editor-edge-label",
            typed?.selected ? "editor-edge-label--selected" : "",
            typed?.staleTarget ? "editor-edge-label--stale" : "",
            "nodrag",
            "nopan",
          ].join(" ")}
          type="button"
          aria-label={accessibleLabel}
          title={accessibleLabel}
          style={{
            "--edge-hue": edgeHue,
            transform: `translate(-50%, -50%) translate(${label.x}px, ${label.y}px)`,
          } as EdgeHueStyle}
          onClick={() => {
            if (typed) {
              typed.onSelect({ type: "transition", stateId: typed.stateId, transitionId: typed.transition.id });
            }
          }}
        >
          <span>{visibleLabel}</span>
          <strong>{target}</strong>
        </button>
      </EdgeLabelRenderer>
    </>
  );
}

function bezierPath({
  sourceX,
  sourceY,
  sourcePosition,
  targetX,
  targetY,
  targetPosition,
}: {
  sourceX: number;
  sourceY: number;
  sourcePosition: EdgeProps<Edge<EditorEdgeData>>["sourcePosition"];
  targetX: number;
  targetY: number;
  targetPosition: EdgeProps<Edge<EditorEdgeData>>["targetPosition"];
}): RenderedRoute {
  const [path, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    curvature: 0.34,
  });
  const points = bezierSamplePoints({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    curvature: 0.34,
  });
  const segments = pointsToSegments(points);
  return {
    path,
    labelX,
    labelY,
    markers: directionMarkers(segments, false),
    segments,
    length: segments.reduce((sum, segment) => sum + segment.length, 0),
  };
}

function perimeterPath({
  sourceX,
  sourceY,
  targetX,
  targetY,
  laneY,
  outboundX,
  inboundX,
  labelX,
  labelY,
}: {
  sourceX: number;
  sourceY: number;
  targetX: number;
  targetY: number;
  laneY: number;
  outboundX: number;
  inboundX: number;
  labelX: number;
  labelY: number;
}): RenderedRoute {
  const radius = 18;
  const points = [
    { x: sourceX, y: sourceY },
    { x: outboundX, y: sourceY },
    { x: outboundX, y: laneY },
    { x: inboundX, y: laneY },
    { x: inboundX, y: targetY },
    { x: targetX, y: targetY },
  ];
  const segments = pointsToSegments(points);
  return {
    path: roundedPolyline(points, radius),
    labelX,
    labelY,
    markers: directionMarkers(segments, true),
    segments,
    length: segments.reduce((sum, segment) => sum + segment.length, 0),
  };
}

function routeLabel(route: RenderedRoute, placement: EditorEdgeData["label"] | undefined): Point {
  if (!placement || route.length <= 0) {
    return { x: route.labelX, y: route.labelY };
  }

  const sample = sampleRoute(route, placement.fraction);
  if (!sample) {
    return { x: placement.x, y: placement.y };
  }
  const normalLength = Math.hypot(sample.segment.dy, sample.segment.dx) || 1;
  const normal = {
    x: -sample.segment.dy / normalLength,
    y: sample.segment.dx / normalLength,
  };
  return {
    x: sample.x + normal.x * placement.offset,
    y: sample.y + normal.y * placement.offset,
  };
}

function sampleRoute(route: RenderedRoute, fraction: number): { x: number; y: number; segment: Segment } | undefined {
  const targetLength = route.length * Math.max(0, Math.min(1, fraction));
  let traversed = 0;
  for (const segment of route.segments) {
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
  const last = route.segments[route.segments.length - 1];
  return last ? { x: last.x1 + last.dx, y: last.y1 + last.dy, segment: last } : undefined;
}

function roundedPolyline(points: Point[], radius: number): string {
  if (points.length < 2) {
    return "";
  }
  const path = [`M ${points[0].x} ${points[0].y}`];
  for (let index = 1; index < points.length; index += 1) {
    const current = points[index];
    const previous = points[index - 1];
    const next = points[index + 1];
    if (!next) {
      path.push(`L ${current.x} ${current.y}`);
      continue;
    }

    const dxIn = current.x - previous.x;
    const dyIn = current.y - previous.y;
    const dxOut = next.x - current.x;
    const dyOut = next.y - current.y;
    const inLength = Math.hypot(dxIn, dyIn);
    const outLength = Math.hypot(dxOut, dyOut);
    const cornerRadius = Math.min(radius, inLength / 2, outLength / 2);
    if (cornerRadius <= 0) {
      path.push(`L ${current.x} ${current.y}`);
      continue;
    }

    const before = {
      x: current.x - (dxIn / inLength) * cornerRadius,
      y: current.y - (dyIn / inLength) * cornerRadius,
    };
    const after = {
      x: current.x + (dxOut / outLength) * cornerRadius,
      y: current.y + (dyOut / outLength) * cornerRadius,
    };
    path.push(`L ${before.x} ${before.y}`);
    path.push(`Q ${current.x} ${current.y} ${after.x} ${after.y}`);
  }
  return path.join(" ");
}

function bezierSamplePoints({
  sourceX,
  sourceY,
  sourcePosition = Position.Bottom,
  targetX,
  targetY,
  targetPosition = Position.Top,
  curvature,
}: {
  sourceX: number;
  sourceY: number;
  sourcePosition?: EdgeProps<Edge<EditorEdgeData>>["sourcePosition"];
  targetX: number;
  targetY: number;
  targetPosition?: EdgeProps<Edge<EditorEdgeData>>["targetPosition"];
  curvature: number;
}): Point[] {
  const source = { x: sourceX, y: sourceY };
  const target = { x: targetX, y: targetY };
  const controlA = controlPoint({
    position: sourcePosition,
    x1: sourceX,
    y1: sourceY,
    x2: targetX,
    y2: targetY,
    curvature,
  });
  const controlB = controlPoint({
    position: targetPosition,
    x1: targetX,
    y1: targetY,
    x2: sourceX,
    y2: sourceY,
    curvature,
  });
  const points: Point[] = [];
  for (let index = 0; index <= 36; index += 1) {
    points.push(cubicBezierPoint(source, controlA, controlB, target, index / 36));
  }
  return points;
}

function controlPoint({
  position = Position.Bottom,
  x1,
  y1,
  x2,
  y2,
  curvature,
}: {
  position?: EdgeProps<Edge<EditorEdgeData>>["sourcePosition"];
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  curvature: number;
}): Point {
  switch (position) {
    case Position.Left:
      return { x: x1 - calculateControlOffset(x1 - x2, curvature), y: y1 };
    case Position.Right:
      return { x: x1 + calculateControlOffset(x2 - x1, curvature), y: y1 };
    case Position.Top:
      return { x: x1, y: y1 - calculateControlOffset(y1 - y2, curvature) };
    case Position.Bottom:
    default:
      return { x: x1, y: y1 + calculateControlOffset(y2 - y1, curvature) };
  }
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

function pointsToSegments(points: Point[]): Segment[] {
  return points.slice(0, -1).flatMap((point, index) => {
    const next = points[index + 1];
    const dx = next.x - point.x;
    const dy = next.y - point.y;
    const length = Math.hypot(dx, dy);
    return length > 0 ? [{ x1: point.x, y1: point.y, dx, dy, length }] : [];
  });
}

function directionMarkers(segments: Segment[], force: boolean): DirectionMarker[] {
  const totalLength = segments.reduce((sum, segment) => sum + segment.length, 0);
  if (!force && totalLength < 520) {
    return [];
  }
  if (totalLength < 240) {
    return [];
  }

  const fractions = force && totalLength > 1500 ? [0.36, 0.7] : [0.68];
  return fractions.flatMap((fraction) => {
    const targetLength = totalLength * fraction;
    let traversed = 0;
    for (const segment of segments) {
      if (traversed + segment.length >= targetLength) {
        const local = segment.length === 0 ? 0 : (targetLength - traversed) / segment.length;
        return [{
          x: segment.x1 + segment.dx * local,
          y: segment.y1 + segment.dy * local,
          angle: Math.atan2(segment.dy, segment.dx) * (180 / Math.PI),
        }];
      }
      traversed += segment.length;
    }
    return [];
  });
}

function transitionLabel(condition: string, target: string, index: number): string {
  const trimmed = condition.trim();
  if (trimmed) {
    return `Decision ${index}: ${trimmed}${target ? `, go ${target}` : ""}`;
  }
  return target ? `Decision ${index}: otherwise, go ${target}` : `Decision ${index}: choose target`;
}
