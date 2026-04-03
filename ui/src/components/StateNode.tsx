import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";

import { formatArgs, formatCompactDuration, formatCountdown, stateSummaryText, truncate } from "../format";
import type { AgentRow } from "../types";
import type { GraphStateNodeData } from "../graph";

export function StateNode({ data }: NodeProps<Node<GraphStateNodeData>>) {
  const state = data.state;
  const mode = data.mode;
  const focusSummary = state.focus;
  const visited = mode === "focus" && !!focusSummary && focusSummary.count > 0;
  const current = data.currentStateName === state.name;
  const isDropTarget = data.dragAgentId !== null && data.dragAgentId !== undefined;
  const bodyRows = [...state.rows.working, ...state.rows.finished];
  const allOverviewRows = [
    ...state.rows.waiting,
    ...bodyRows,
    ...state.rows.paused,
    ...state.rows.needs_help,
  ];
  const singleAgentId = mode === "overview" && allOverviewRows.length === 1 ? allOverviewRows[0]?.id : null;

  return (
    <div
      className={stateNodeClassName(mode, visited, current)}
      onDragOver={(event) => {
        if (mode !== "focus" || !data.dragAgentId) {
          return;
        }
        event.preventDefault();
      }}
      onDrop={(event) => {
        if (mode !== "focus" || !data.dragAgentId || !data.onMoveAgent) {
          return;
        }
        event.preventDefault();
        data.onMoveAgent(state.name);
        data.onDragAgent?.(null);
      }}
    >
      {mode === "overview" ? (
        <>
          <Shelf title="Waiting" tone="waiting" rows={state.rows.waiting} onSelectAgent={data.onSelectAgent} />
          <div
            className={[
              "state-node__body",
              "state-node__body--overview",
              singleAgentId ? "state-node__body--interactive" : "",
            ].join(" ")}
            onClick={() => {
              if (singleAgentId) {
                data.onSelectAgent?.(singleAgentId);
              }
            }}
          >
            <Handle type="target" position={Position.Left} className="flow-handle" />
            <StateTitle name={state.name} start={state.start} end={state.end} />
            <RowList rows={bodyRows} onSelectAgent={data.onSelectAgent} />
            <Handle type="source" position={Position.Right} className="flow-handle" />
          </div>
          <Shelf title="Paused" tone="paused" rows={state.rows.paused} onSelectAgent={data.onSelectAgent} />
          <Shelf title="Needs Help" tone="needs-help" rows={state.rows.needs_help} onSelectAgent={data.onSelectAgent} />
        </>
      ) : (
        <>
          <div className="state-node__body state-node__body--focus">
            <Handle type="target" position={Position.Left} className="flow-handle" />
            <StateTitle name={state.name} start={state.start} end={state.end} />
            {visited ? (
              <button
                className="focus-summary nopan"
                type="button"
                onMouseEnter={() => data.onHoverKey?.(state.name)}
                onMouseLeave={() => data.onHoverKey?.(null)}
                onClick={() => data.onPinKey?.(state.name)}
              >
                {stateSummaryText(focusSummary)}
              </button>
            ) : (
              <div className="focus-summary focus-summary--ghost">unvisited</div>
            )}
            {visited ? (
              <div className="history-popover">
                <div className="history-popover__title">State visits</div>
                <div className="history-popover__list nowheel">
                  {focusSummary?.visits.map((visit) => (
                    <div className="history-popover__item" key={visit.id}>
                      <span>{visit.duration_text}</span>
                      <span>{visit.started_at.replace("T", " ").replace("Z", "")}</span>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
            <Handle type="source" position={Position.Right} className="flow-handle" />
          </div>
          {current ? (
            <button
              className={["pause-drop", isDropTarget ? "pause-drop--armed" : ""].join(" ")}
              type="button"
              onDragOver={(event) => {
                if (!data.dragAgentId) {
                  return;
                }
                event.preventDefault();
              }}
              onDrop={(event) => {
                if (!data.dragAgentId) {
                  return;
                }
                event.preventDefault();
                data.onPauseAgent?.();
                data.onDragAgent?.(null);
              }}
            >
              drop here to pause
            </button>
          ) : (
            <div className="pause-drop pause-drop--ghost" />
          )}
        </>
      )}
    </div>
  );
}

export function stateNodeClassName(mode: "overview" | "focus", visited: boolean, current: boolean): string {
  return [
    "state-node",
    mode === "focus" ? "state-node--focus" : "state-node--overview",
    mode === "focus" ? (visited ? "state-node--visited" : "state-node--ghost") : "",
    current ? "state-node--current" : "",
    "nopan",
  ]
    .filter(Boolean)
    .join(" ");
}

function StateTitle({ name, start, end }: { name: string; start: boolean; end: boolean }) {
  return (
    <div className="state-node__header">
      <div className="state-node__title">{name}</div>
      <div className="state-node__badges">
        {start ? <span className="state-badge">start</span> : null}
        {end ? <span className="state-badge state-badge--end">end</span> : null}
      </div>
    </div>
  );
}

function Shelf({
  title,
  tone,
  rows,
  onSelectAgent,
}: {
  title: string;
  tone: "waiting" | "paused" | "needs-help";
  rows: AgentRow[];
  onSelectAgent?: (agentId: number) => void;
}) {
  if (rows.length === 0) {
    return null;
  }
  return (
    <div className={`state-shelf state-shelf--${tone}`}>
      <div className="state-shelf__label">{title}</div>
      <RowList rows={rows} tone={tone} onSelectAgent={onSelectAgent} />
    </div>
  );
}

function RowList({
  rows,
  tone,
  onSelectAgent,
}: {
  rows: AgentRow[];
  tone?: "waiting" | "working" | "paused" | "needs-help";
  onSelectAgent?: (agentId: number) => void;
}) {
  if (rows.length === 0) {
    return <div className="row-list row-list--empty"> </div>;
  }
  return (
    <div className="row-list nowheel">
      {rows.map((row) => (
        <button
          key={row.id}
          className={`agent-pill agent-pill--${pillTone(row, tone)}`}
          type="button"
          onClick={() => onSelectAgent?.(row.id)}
        >
          <span className="agent-pill__id">#{row.id}</span>
          <span className="agent-pill__args">{truncate(formatArgs(row.display_args), 36)}</span>
          <span className="agent-pill__timer">
            {row.status === "waiting" ? formatCountdown(row.timer_seconds) : formatCompactDuration(row.timer_seconds)}
          </span>
        </button>
      ))}
    </div>
  );
}

function pillTone(row: AgentRow, tone?: "waiting" | "working" | "paused" | "needs-help"): string {
  if (tone) {
    return tone;
  }
  if (row.status === "needs_help") {
    return "needs-help";
  }
  return row.status;
}
