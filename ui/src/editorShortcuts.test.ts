import { editorShortcutAction } from "./editorShortcuts";
import type { EditorDocument } from "./types";

function makeDocument(): EditorDocument {
  return {
    path: "/Users/moconnor/flows/demo.yaml",
    fileName: "demo.yaml",
    yaml: "",
    flow: {
      name: "demo",
      description: "",
      version: "",
      path: "",
      mode: "",
      thinking: "",
      fast: null,
      args: [],
    },
    states: [
      {
        id: "state-input",
        name: "input",
        start: true,
        end: false,
        prompt: "",
        wait: "",
        mode: "",
        thinking: "",
        fast: null,
        transitions: [
          { id: "transition-one", condition: "one", wait: "", target: "first" },
          { id: "transition-two", condition: "two", wait: "", target: "second" },
        ],
      },
      {
        id: "state-first",
        name: "first",
        start: false,
        end: true,
        prompt: "",
        wait: "",
        mode: "",
        thinking: "",
        fast: null,
        transitions: [],
      },
      {
        id: "state-second",
        name: "second",
        start: false,
        end: true,
        prompt: "",
        wait: "",
        mode: "",
        thinking: "",
        fast: null,
        transitions: [],
      },
    ],
    validation: { ok: true, errors: [], warnings: [] },
  };
}

describe("editorShortcutAction", () => {
  it("uses 0 to select the input state from anywhere", () => {
    expect(editorShortcutAction(makeDocument(), { type: "flow" }, "0")).toEqual({
      type: "select-state",
      stateId: "state-input",
    });
  });

  it("selects a numbered transition from the selected state", () => {
    expect(editorShortcutAction(makeDocument(), { type: "state", stateId: "state-input" }, "2")).toEqual({
      type: "select-transition",
      stateId: "state-input",
      transitionId: "transition-two",
    });
  });

  it("switches to another numbered transition from the same source state", () => {
    expect(
      editorShortcutAction(
        makeDocument(),
        { type: "transition", stateId: "state-input", transitionId: "transition-one" },
        "2",
      ),
    ).toEqual({
      type: "select-transition",
      stateId: "state-input",
      transitionId: "transition-two",
    });
  });

  it("follows the selected transition when the same number is pressed again", () => {
    expect(
      editorShortcutAction(
        makeDocument(),
        { type: "transition", stateId: "state-input", transitionId: "transition-two" },
        "2",
      ),
    ).toEqual({
      type: "follow-transition",
      stateId: "state-input",
      transitionId: "transition-two",
      targetStateId: "state-second",
    });
  });

  it("ignores unsupported keys and missing decisions", () => {
    expect(editorShortcutAction(makeDocument(), { type: "state", stateId: "state-input" }, "9")).toEqual({
      type: "none",
    });
    expect(editorShortcutAction(makeDocument(), { type: "state", stateId: "state-input" }, "x")).toEqual({
      type: "none",
    });
  });
});
