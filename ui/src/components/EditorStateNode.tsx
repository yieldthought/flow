import type { CSSProperties } from "react";
import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";

import {
  VISIBLE_DECISION_HANDLES,
  editorOverflowHandleId,
  editorStateHue,
  editorTransitionHandleId,
  type EditorMissingNodeData,
  type EditorStateNodeData,
} from "../editorGraph";

type HueStyle = CSSProperties & {
  "--state-hue"?: number;
  "--decision-hue"?: number;
};

export function EditorStateNode({ data }: NodeProps<Node<EditorStateNodeData>>) {
  const state = data.state;
  const promptSummary = state.prompt.trim() || "No prompt";
  const transitionCount = state.transitions.length;
  const selectedTransitionId = data.selectedTransitionId;

  return (
    <section
      className={["editor-node", data.selected ? "editor-node--selected" : ""].join(" ")}
      style={{ "--state-hue": editorStateHue(state.name) } as HueStyle}
      onClick={() => data.onSelect({ type: "state", stateId: state.id })}
    >
      <Handle type="target" position={Position.Left} className="flow-handle editor-handle" />
      <div className="editor-node__top">
        <input
          className="editor-node__title nodrag nopan"
          value={state.name}
          aria-label="State name"
          onChange={(event) => data.onUpdateState(state.id, { name: event.target.value })}
          onFocus={() => data.onSelect({ type: "state", stateId: state.id })}
        />
        <div className="editor-node__badges">
          {state.start ? <span className="state-badge state-badge--start">start</span> : null}
          {state.end ? <span className="state-badge state-badge--end">end</span> : null}
          {data.issueCount > 0 ? <span className="state-badge state-badge--issue">{data.issueCount}</span> : null}
        </div>
      </div>
      <button
        className="editor-node__prompt nodrag nopan"
        type="button"
        onClick={() => data.onSelect({ type: "state", stateId: state.id })}
      >
        {promptSummary}
      </button>
      {state.transitions.length ? (
        <div className="editor-node__decisions">
          {state.transitions.slice(0, VISIBLE_DECISION_HANDLES).map((transition, index) => (
            <div
              className="editor-node__decision-anchor"
              key={transition.id}
              style={{ "--decision-hue": editorStateHue(transition.target) } as HueStyle}
            >
              <button
                className={[
                  "editor-node__decision",
                  transition.id === selectedTransitionId ? "editor-node__decision--selected" : "",
                  "nodrag",
                  "nopan",
                ].join(" ")}
                type="button"
                title={transition.condition || `otherwise go ${transition.target}`}
                onClick={(event) => {
                  event.stopPropagation();
                  data.onSelect({ type: "transition", stateId: state.id, transitionId: transition.id });
                }}
              >
                <span className="editor-node__decision-index">{index + 1}</span>
                <span className="editor-node__decision-text">{transition.condition || "otherwise"}</span>
                <span className="editor-node__decision-target">{transition.target}</span>
              </button>
              <Handle
                id={editorTransitionHandleId(transition.id)}
                type="source"
                position={Position.Right}
                className="flow-handle editor-handle editor-handle--decision"
              />
            </div>
          ))}
          {state.transitions.length > VISIBLE_DECISION_HANDLES ? (
            <div
              className="editor-node__decision-anchor editor-node__decision-anchor--more"
              style={{ "--decision-hue": editorStateHue(state.name) } as HueStyle}
            >
              <button
                className="editor-node__decision editor-node__decision--more nodrag nopan"
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  data.onSelect({ type: "state", stateId: state.id });
                }}
              >
                {state.transitions.length - VISIBLE_DECISION_HANDLES} more decisions
              </button>
              <Handle
                id={editorOverflowHandleId(state.id)}
                type="source"
                position={Position.Right}
                className="flow-handle editor-handle editor-handle--decision"
              />
            </div>
          ) : null}
        </div>
      ) : null}
      <div className="editor-node__meta">
        <span>{transitionCount} decision{transitionCount === 1 ? "" : "s"}</span>
        {state.wait ? <span>wait {state.wait}</span> : null}
        {state.thinking ? <span>{state.thinking}</span> : null}
      </div>
      <div className="editor-node__actions">
        <button
          className="icon-button nodrag nopan"
          type="button"
          title="Add decision"
          aria-label="Add decision"
          onClick={(event) => {
            event.stopPropagation();
            data.onAddTransition(state.id);
          }}
        >
          +
        </button>
        <button
          className="icon-button icon-button--danger nodrag nopan"
          type="button"
          title="Delete state"
          aria-label="Delete state"
          onClick={(event) => {
            event.stopPropagation();
            data.onDeleteState(state.id);
          }}
        >
          -
        </button>
      </div>
    </section>
  );
}

export function EditorMissingNode({ data }: NodeProps<Node<EditorMissingNodeData>>) {
  return (
    <section className="editor-node editor-node--missing" style={{ "--state-hue": editorStateHue(data.name) } as HueStyle}>
      <Handle type="target" position={Position.Left} className="flow-handle editor-handle" />
      <div className="editor-node__missing-label">Missing target</div>
      <div className="editor-node__missing-name">{data.name}</div>
    </section>
  );
}
