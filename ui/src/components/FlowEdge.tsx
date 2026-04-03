import { BaseEdge, EdgeLabelRenderer, type Edge, type EdgeProps } from "@xyflow/react";

import { edgeSummaryText, edgeTransitionText, truncate } from "../format";
import type { GraphEdgeData } from "../graph";

export function FlowEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  data,
}: EdgeProps<Edge<GraphEdgeData>>) {
  const typed = data as GraphEdgeData | undefined;
  const focus = typed?.edge.focus ?? null;
  const visited = !!focus;
  const edgeKey = typed?.edge.key ?? id;
  const transitionText = edgeTransitionText(typed?.edge.transition_labels, typed?.edge.transition_label_text ?? "");
  const waitText = bubbleWaitText(transitionText);
  const route = buildRoute({
    sourceX,
    sourceY,
    targetX,
    targetY,
    detourY: typed?.detourY,
    selfLoop: typed?.selfLoop,
  });

  return (
    <>
      <BaseEdge
        id={id}
        path={route.path}
        style={{ stroke: visited ? "#87c3ff" : "#8fa0b8", strokeWidth: visited ? 1.8 : 1.1 }}
      />
      <EdgeLabelRenderer>
        {waitText ? (
          <div
            className="edge-badge edge-badge--wait"
            style={{
              transform: `translate(-50%, -100%) translate(${route.labelX}px, ${route.labelY - 10}px)`,
            }}
          >
            {truncate(waitText, 32)}
          </div>
        ) : null}
        <div
          className="edge-marker"
          style={{
            transform: `translate(-50%, -50%) translate(${route.labelX}px, ${route.labelY}px) rotate(${route.labelAngle}deg)`,
          }}
          aria-hidden="true"
        >
          <svg className="edge-marker__triangle" viewBox="0 0 10 10">
            <path d="M 1 1 L 9 5 L 1 9 z" />
          </svg>
        </div>
        {typed?.mode === "focus" && focus ? (
          <div
            className="edge-badge edge-badge--summary"
            style={{
              transform: `translate(-50%, 0) translate(${route.labelX}px, ${route.labelY + 10}px)`,
            }}
            onMouseEnter={() => {
              typed?.onHoverKey?.(edgeKey);
            }}
            onMouseLeave={() => {
              typed?.onHoverKey?.(null);
            }}
            onClick={() => {
              typed?.onPinKey?.(edgeKey);
            }}
          >
            <div className="edge-badge__summary">{truncate(edgeSummaryText(focus), 72)}</div>
            <div className="history-popover history-popover--edge">
              <div className="history-popover__title">Edge history</div>
              <div className="history-popover__list nowheel">
                {focus.items.map((item) => (
                  <div className="history-popover__item" key={item.id}>
                    <span>{item.absolute_time_text}</span>
                    <span>{truncate(item.reason || item.choice, 44)}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        ) : null}
      </EdgeLabelRenderer>
    </>
  );
}

function buildRoute({
  sourceX,
  sourceY,
  targetX,
  targetY,
  detourY,
  selfLoop,
}: {
  sourceX: number;
  sourceY: number;
  targetX: number;
  targetY: number;
  detourY?: number;
  selfLoop?: {
    left: number;
    right: number;
    top: number;
    bottom: number;
  };
}): { path: string; labelX: number; labelY: number; labelAngle: number } {
  if (selfLoop) {
    const sideOffset = 22;
    const topOffset = 30;
    const loopTop = selfLoop.top - topOffset;
    const loopLeft = selfLoop.left - sideOffset;
    const loopRight = selfLoop.right + sideOffset;
    const points = [
      [sourceX, sourceY],
      [loopRight, sourceY],
      [loopRight, loopTop],
      [loopLeft, loopTop],
      [loopLeft, targetY],
      [targetX, targetY],
    ];
    return routeGeometry(points);
  }

  const outerOffset = 18;
  const points = detourY === undefined
    ? [
        [sourceX, sourceY],
        [(sourceX + targetX) / 2, sourceY],
        [(sourceX + targetX) / 2, targetY],
        [targetX, targetY],
      ]
    : [
        [sourceX, sourceY],
        [sourceX + outerOffset, sourceY],
        [sourceX + outerOffset, detourY],
        [targetX - outerOffset, detourY],
        [targetX - outerOffset, targetY],
        [targetX, targetY],
      ];

  return routeGeometry(points);
}

function pointsToPath(points: number[][]): string {
  return points
    .map(([x, y], index) => `${index === 0 ? "M" : "L"} ${x} ${y}`)
    .join(" ");
}

function routeGeometry(points: number[][]): { path: string; labelX: number; labelY: number; labelAngle: number } {
  const segments = points.slice(0, -1).map(([x1, y1], index) => {
    const [x2, y2] = points[index + 1];
    const dx = x2 - x1;
    const dy = y2 - y1;
    return {
      x1,
      y1,
      x2,
      y2,
      dx,
      dy,
      length: Math.hypot(dx, dy),
    };
  }).filter((segment) => segment.length > 0);

  const totalLength = segments.reduce((sum, segment) => sum + segment.length, 0);
  const halfway = totalLength / 2;
  let traversed = 0;
  let active = segments[segments.length - 1];
  let offset = active.length;

  for (const segment of segments) {
    if (traversed + segment.length >= halfway) {
      active = segment;
      offset = halfway - traversed;
      break;
    }
    traversed += segment.length;
  }

  const ratio = active.length === 0 ? 0 : offset / active.length;
  const labelX = active.x1 + active.dx * ratio;
  const labelY = active.y1 + active.dy * ratio;
  const labelAngle = Math.atan2(active.dy, active.dx) * (180 / Math.PI);

  return {
    path: pointsToPath(points),
    labelX,
    labelY,
    labelAngle,
  };
}

function bubbleWaitText(value: string): string {
  const trimmed = value.trim();
  const match = trimmed.match(/^\[(.+)\]$/);
  return match ? match[1] : trimmed;
}
