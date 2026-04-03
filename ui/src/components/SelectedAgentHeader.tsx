import { formatArgs, formatCompactDuration, formatCountdown, formatStatus } from "../format";
import type { FocusAgent } from "../types";

interface Props {
  agent: FocusAgent;
  moveTarget: string;
  busy: string | null;
  onMoveTargetChange: (value: string) => void;
  onAction: (action: "pause" | "interrupt" | "resume" | "wake" | "stop" | "move") => void;
  onDragStart: (agentId: number) => void;
  onDragEnd: () => void;
  onBack: () => void;
}

export function SelectedAgentHeader({
  agent,
  moveTarget,
  busy,
  onMoveTargetChange,
  onAction,
  onDragStart,
  onDragEnd,
  onBack,
}: Props) {
  const timerText = agent.status === "waiting" ? formatCountdown(agent.timer_seconds) : formatCompactDuration(agent.timer_seconds);
  const disabled = !!busy;

  return (
    <div
      className="selected-header"
      draggable={!disabled}
      onDragStart={() => onDragStart(agent.id)}
      onDragEnd={onDragEnd}
    >
      <div className="selected-header__meta">
        <button className="ghost-button" type="button" onClick={onBack}>
          overview
        </button>
        <div className="selected-header__identity">
          <span className="agent-pill agent-pill--working agent-pill--header">
            <span className="agent-pill__id">#{agent.id}</span>
            <span className="agent-pill__args">{formatArgs(agent.display_args)}</span>
            <span className="agent-pill__timer">{timerText}</span>
          </span>
          <div className="selected-header__details">
            <div>
              {agent.current_state} · {formatStatus(agent.status)} · {agent.phase} · {agent.substate}
            </div>
            <div>{agent.cwd}</div>
            <div>{formatArgs(agent.args)}</div>
            {agent.status_message ? <div className="selected-header__status">{agent.status_message}</div> : null}
          </div>
        </div>
      </div>
      <div className="selected-header__actions">
        <ActionButton label="pause" disabled={disabled || !!agent.ended_at || agent.substate !== "normal"} onClick={() => onAction("pause")} />
        <ActionButton label="interrupt" disabled={disabled || !!agent.ended_at || agent.substate !== "normal"} onClick={() => onAction("interrupt")} />
        <ActionButton label="resume" disabled={disabled || !!agent.ended_at || agent.substate === "normal"} onClick={() => onAction("resume")} />
        <ActionButton label="wake" disabled={disabled || !agent.ready_at} onClick={() => onAction("wake")} />
        <div className="move-controls">
          <select value={moveTarget} onChange={(event) => onMoveTargetChange(event.target.value)} disabled={disabled || !!agent.ended_at}>
            {agent.state_options
              .filter((name) => name !== agent.current_state)
              .map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
          </select>
          <ActionButton
            label="move"
            disabled={disabled || !!agent.ended_at || !moveTarget}
            onClick={() => onAction("move")}
          />
        </div>
        <ActionButton label="stop" tone="danger" disabled={disabled || !!agent.ended_at} onClick={() => onAction("stop")} />
      </div>
    </div>
  );
}

function ActionButton({
  label,
  disabled,
  onClick,
  tone = "default",
}: {
  label: string;
  disabled: boolean;
  onClick: () => void;
  tone?: "default" | "danger";
}) {
  return (
    <button className={`action-button action-button--${tone}`} type="button" disabled={disabled} onClick={onClick}>
      {label}
    </button>
  );
}
