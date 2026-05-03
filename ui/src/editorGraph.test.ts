import { buildEditorGraphModel, editorStateHue, editorTransitionHandleId } from "./editorGraph";
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
        id: "state-check",
        name: "check",
        start: true,
        end: false,
        prompt: "Check",
        wait: "",
        mode: "",
        thinking: "",
        fast: null,
        transitions: [{ id: "transition-done", condition: "done", wait: "", target: "done" }],
      },
      {
        id: "state-done",
        name: "done",
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

describe("editor graph model", () => {
  it("renders states and transitions from an editor document", () => {
    const graph = buildEditorGraphModel({
      document: makeDocument(),
      selection: { type: "flow" },
      onSelect: () => undefined,
      onUpdateState: () => undefined,
      onAddTransition: () => undefined,
      onDeleteState: () => undefined,
    });

    expect(graph.nodes.map((node) => node.id)).toEqual(["state-check", "state-done"]);
    expect(graph.edges[0]?.source).toBe("state-check");
    expect(graph.edges[0]?.sourceHandle).toBe(editorTransitionHandleId("transition-done"));
    expect(graph.edges[0]?.target).toBe("state-done");
    expect(graph.edges[0]?.data?.targetHue).toBe(editorStateHue("done"));
  });

  it("adds a synthetic node for a missing transition target", () => {
    const document = makeDocument();
    document.states[0].transitions[0].target = "missing";
    const graph = buildEditorGraphModel({
      document,
      selection: { type: "flow" },
      onSelect: () => undefined,
      onUpdateState: () => undefined,
      onAddTransition: () => undefined,
      onDeleteState: () => undefined,
    });

    expect(graph.nodes.some((node) => node.id === "missing:missing")).toBe(true);
    expect(graph.edges[0]?.data?.staleTarget).toBe(true);
  });

  it("routes feedback transitions around the graph perimeter", () => {
    const document = makeDocument();
    document.states[1] = {
      ...document.states[1],
      end: false,
      transitions: [{ id: "transition-repeat", condition: "try again", wait: "", target: "check" }],
    };

    const graph = buildEditorGraphModel({
      document,
      selection: { type: "flow" },
      onSelect: () => undefined,
      onUpdateState: () => undefined,
      onAddTransition: () => undefined,
      onDeleteState: () => undefined,
    });

    const forward = graph.edges.find((edge) => edge.id === "state-check-transition-done");
    const feedback = graph.edges.find((edge) => edge.id === "state-done-transition-repeat");

    expect(forward?.data?.route).toBeUndefined();
    expect(feedback?.data?.route?.kind).toBe("perimeter");
    expect(feedback?.data?.route?.laneY).not.toBeUndefined();
  });

  it("marks the source node decision when a transition is selected", () => {
    const graph = buildEditorGraphModel({
      document: makeDocument(),
      selection: { type: "transition", stateId: "state-check", transitionId: "transition-done" },
      onSelect: () => undefined,
      onUpdateState: () => undefined,
      onAddTransition: () => undefined,
      onDeleteState: () => undefined,
    });

    const source = graph.nodes.find((node) => node.id === "state-check");
    const target = graph.nodes.find((node) => node.id === "state-done");

    expect(source?.data?.selected).toBe(true);
    expect(source?.data?.selectedTransitionId).toBe("transition-done");
    expect(target?.data?.selected).toBe(false);
  });
});
