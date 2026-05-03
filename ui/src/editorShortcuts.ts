import type { EditorDocument } from "./types";
import type { EditorSelection } from "./editorGraph";

export type EditorShortcutAction =
  | { type: "none" }
  | { type: "select-state"; stateId: string }
  | { type: "select-transition"; stateId: string; transitionId: string }
  | { type: "follow-transition"; stateId: string; transitionId: string; targetStateId: string };

export function editorShortcutAction(
  document: EditorDocument | null,
  selection: EditorSelection,
  key: string,
): EditorShortcutAction {
  if (!document || !/^[0-9]$/.test(key)) {
    return { type: "none" };
  }

  if (key === "0") {
    const inputState = document.states.find((state) => state.start) ?? document.states[0];
    return inputState ? { type: "select-state", stateId: inputState.id } : { type: "none" };
  }

  const sourceStateId = selection.type === "state" || selection.type === "transition"
    ? selection.stateId
    : "";
  const sourceState = document.states.find((state) => state.id === sourceStateId);
  if (!sourceState) {
    return { type: "none" };
  }

  const transitionIndex = Number(key) - 1;
  const transition = sourceState.transitions[transitionIndex];
  if (!transition) {
    return { type: "none" };
  }

  if (selection.type === "transition" && selection.transitionId === transition.id) {
    const targetState = document.states.find((state) => state.name === transition.target);
    return targetState
      ? {
          type: "follow-transition",
          stateId: sourceState.id,
          transitionId: transition.id,
          targetStateId: targetState.id,
        }
      : { type: "none" };
  }

  return { type: "select-transition", stateId: sourceState.id, transitionId: transition.id };
}
