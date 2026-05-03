import {
  Background,
  Controls,
  PanOnScrollMode,
  ReactFlow,
  useReactFlow,
  type Edge,
  type EdgeTypes,
  type Node,
  type NodeTypes,
} from "@xyflow/react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import {
  fetchEditorDocument,
  fetchEditorFiles,
  saveEditorDocument,
  validateEditorDocument,
} from "./api";
import { EditorEdge } from "./components/EditorEdge";
import { EditorMissingNode, EditorStateNode } from "./components/EditorStateNode";
import { buildEditorGraphModel, type EditorSelection } from "./editorGraph";
import { editorShortcutAction } from "./editorShortcuts";
import type {
  EditorArg,
  EditorDocument,
  EditorFileEntry,
  EditorState,
  EditorTransition,
  EditorValidation,
} from "./types";

const editorNodeTypes: NodeTypes = {
  "editor-state": EditorStateNode,
  "editor-missing": EditorMissingNode,
};
const editorEdgeTypes: EdgeTypes = { "editor-edge": EditorEdge };

const MODE_OPTIONS = ["", "yolo", "danger-full-access", "full-auto", "workspace-write"];
const THINKING_OPTIONS = ["", "low", "medium", "high", "xhigh"];
const SHORTCUT_PAN_DURATION_MS = 340;
const SHORTCUT_MIN_ZOOM = 0.72;
const FALLBACK_NODE_WIDTH = 480;
const FALLBACK_NODE_HEIGHT = 300;

type ViewportAnchor = {
  stateId: string;
  screenX: number;
  screenY: number;
  zoom: number;
};

export function VisualEditorApp({
  apiBaseUrl,
  preferredFlowName,
}: {
  apiBaseUrl: string;
  preferredFlowName?: string;
}) {
  const reactFlow = useReactFlow();
  const [files, setFiles] = useState<EditorFileEntry[]>([]);
  const [document, setDocument] = useState<EditorDocument | null>(null);
  const [selection, setSelection] = useState<EditorSelection>({ type: "flow" });
  const [query, setQuery] = useState("");
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<string | null>("loading");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const viewportAnchorRef = useRef<ViewportAnchor | null>(null);
  const shortcutPanTimerRef = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setBusy("loading");
      try {
        const payload = await fetchEditorFiles(apiBaseUrl);
        if (cancelled) {
          return;
        }
        setFiles(payload.files);
        const params = new URLSearchParams(window.location.search);
        const preferredPath = params.get("file") ?? "";
        const preferredName = params.get("name") ?? preferredFlowName ?? "";
        const preferred = preferredPath
          ? payload.files.find((file) => file.path === preferredPath)
          : preferredName
            ? payload.files.find((file) => file.name === preferredName)
            : undefined;
        if (preferredName || preferredPath) {
          setQuery(preferred?.name ?? preferredName);
        }
        const firstUserFlow = payload.files.find((file) => file.path.includes("/flows/"));
        const first = preferred ?? firstUserFlow ?? payload.files[0];
        if (first) {
          await openFile(first.path, { quiet: true });
        }
        setError("");
      } catch (exc) {
        if (!cancelled) {
          setError(exc instanceof Error ? exc.message : "Failed to load flow files");
        }
      } finally {
        if (!cancelled) {
          setBusy(null);
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [apiBaseUrl, preferredFlowName]);

  const editableSignature = useMemo(
    () => (document ? JSON.stringify({ flow: document.flow, states: document.states }) : ""),
    [document?.flow, document?.states],
  );

  useEffect(() => {
    if (!document || !dirty) {
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void validateEditorDocument(apiBaseUrl, document)
        .then((validation) => {
          if (!cancelled) {
            setDocument((current) => (current && current.path === document.path ? { ...current, validation } : current));
          }
        })
        .catch((exc) => {
          if (!cancelled) {
            setError(exc instanceof Error ? exc.message : "Failed to validate flow");
          }
        });
    }, 280);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [apiBaseUrl, dirty, editableSignature]);

  useEffect(() => {
    if (!document) {
      return;
    }
    const validSelection = selectionExists(document, selection);
    if (!validSelection) {
      setSelection({ type: "flow" });
    }
  }, [document, selection]);

  async function openFile(path: string, options: { quiet?: boolean } = {}) {
    if (dirty && !options.quiet) {
      setNotice("Unsaved edits remain in the current file.");
      return;
    }
    setBusy("opening");
    try {
      const next = await fetchEditorDocument(apiBaseUrl, path);
      setDocument(next);
      setSelection({ type: "flow" });
      setDirty(false);
      setNotice("");
      setError("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to open flow file");
    } finally {
      setBusy(null);
    }
  }

  async function save() {
    if (!document) {
      return;
    }
    setBusy("saving");
    try {
      const saved = await saveEditorDocument(apiBaseUrl, document);
      setDocument(saved);
      setDirty(false);
      setNotice("Saved");
      setError("");
      const payload = await fetchEditorFiles(apiBaseUrl);
      setFiles(payload.files);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to save flow");
    } finally {
      setBusy(null);
    }
  }

  async function revert() {
    if (!document) {
      return;
    }
    await openFile(document.path, { quiet: true });
  }

  function editDocument(mutator: (current: EditorDocument) => EditorDocument, anchorStateId?: string) {
    if (anchorStateId) {
      captureViewportAnchor(anchorStateId);
    }
    setDocument((current) => {
      if (!current) {
        return current;
      }
      return mutator(current);
    });
    setDirty(true);
    setNotice("");
  }

  function updateFlow(patch: Partial<EditorDocument["flow"]>) {
    editDocument((current) => ({ ...current, flow: { ...current.flow, ...patch } }));
  }

  function updateState(stateId: string, patch: Partial<EditorState>) {
    editDocument((current) => {
      const original = current.states.find((state) => state.id === stateId);
      const oldName = original?.name ?? "";
      const newName = patch.name ?? oldName;
      const states = current.states.map((state) => {
        if (state.id === stateId) {
          const next = { ...state, ...patch };
          return patch.end === true ? { ...next, transitions: [] } : next;
        }
        if (patch.name !== undefined && oldName && newName) {
          return {
            ...state,
            transitions: state.transitions.map((transition) =>
              transition.target === oldName ? { ...transition, target: newName } : transition,
            ),
          };
        }
        return state;
      });
      return { ...current, states };
    }, stateId);
  }

  function updateTransition(stateId: string, transitionId: string, patch: Partial<EditorTransition>) {
    editDocument((current) => ({
      ...current,
      states: current.states.map((state) =>
        state.id === stateId
          ? {
              ...state,
              transitions: state.transitions.map((transition) =>
                transition.id === transitionId ? { ...transition, ...patch } : transition,
              ),
            }
          : state,
      ),
    }), stateId);
  }

  function addState() {
    editDocument((current) => {
      const name = uniqueStateName(current.states, "new-state");
      const state: EditorState = {
        id: makeId("state"),
        name,
        start: current.states.length === 0,
        end: false,
        prompt: "",
        wait: "",
        mode: "",
        thinking: "",
        fast: null,
        transitions: [],
      };
      setSelection({ type: "state", stateId: state.id });
      return { ...current, states: [...current.states, state] };
    });
  }

  function deleteState(stateId: string) {
    editDocument((current) => {
      if (current.states.length <= 1) {
        return current;
      }
      const deleted = current.states.find((state) => state.id === stateId);
      const deletedName = deleted?.name ?? "";
      const states = current.states
        .filter((state) => state.id !== stateId)
        .map((state) => ({
          ...state,
          transitions: state.transitions.filter((transition) => transition.target !== deletedName),
        }));
      setSelection({ type: "flow" });
      return { ...current, states };
    });
  }

  function addTransition(stateId: string) {
    editDocument((current) => {
      const source = current.states.find((state) => state.id === stateId);
      if (!source) {
        return current;
      }
      const target = current.states.find((state) => state.id !== stateId)?.name ?? source.name;
      const transition: EditorTransition = {
        id: makeId("transition"),
        condition: "",
        wait: "",
        target,
      };
      setSelection({ type: "transition", stateId, transitionId: transition.id });
      return {
        ...current,
        states: current.states.map((state) =>
          state.id === stateId
            ? { ...state, end: false, transitions: [...state.transitions, transition] }
            : state,
        ),
      };
    }, stateId);
  }

  function deleteTransition(stateId: string, transitionId: string) {
    editDocument((current) => ({
      ...current,
      states: current.states.map((state) =>
        state.id === stateId
          ? { ...state, transitions: state.transitions.filter((transition) => transition.id !== transitionId) }
          : state,
      ),
    }), stateId);
    setSelection({ type: "state", stateId });
  }

  function updateArg(index: number, patch: Partial<EditorArg>) {
    editDocument((current) => ({
      ...current,
      flow: {
        ...current.flow,
        args: current.flow.args.map((arg, itemIndex) => (itemIndex === index ? { ...arg, ...patch } : arg)),
      },
    }));
  }

  function addArg() {
    editDocument((current) => ({
      ...current,
      flow: {
        ...current.flow,
        args: [...current.flow.args, { name: uniqueArgName(current.flow.args), help: "", default: "" }],
      },
    }));
  }

  function deleteArg(index: number) {
    editDocument((current) => ({
      ...current,
      flow: { ...current.flow, args: current.flow.args.filter((_, itemIndex) => itemIndex !== index) },
    }));
  }

  const filteredFiles = files.filter((file) => {
    const haystack = `${file.name} ${file.description} ${file.path}`.toLowerCase();
    return haystack.includes(query.trim().toLowerCase());
  });

  const graph = useMemo(() => {
    if (!document) {
      return { nodes: [] as Node[], edges: [] as Edge[] };
    }
    return buildEditorGraphModel({
      document,
      selection,
      onSelect: setSelection,
      onUpdateState: updateState,
      onAddTransition: addTransition,
      onDeleteState: deleteState,
    });
  }, [document, selection]);

  useEffect(() => {
    return () => clearShortcutPanTimer();
  }, []);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (
        event.defaultPrevented ||
        event.repeat ||
        event.metaKey ||
        event.ctrlKey ||
        event.altKey ||
        isEditableShortcutTarget(event.target)
      ) {
        return;
      }

      const action = editorShortcutAction(document, selection, event.key);
      if (action.type === "none") {
        return;
      }

      event.preventDefault();
      clearShortcutPanTimer();

      if (action.type === "select-state") {
        focusState(action.stateId, { selectAfterPan: false });
        return;
      }

      if (action.type === "select-transition") {
        setSelection({ type: "transition", stateId: action.stateId, transitionId: action.transitionId });
        return;
      }

      focusState(action.targetStateId, { selectAfterPan: true });
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [document, selection, graph.nodes, reactFlow]);

  useLayoutEffect(() => {
    const anchor = viewportAnchorRef.current;
    if (!anchor) {
      return;
    }
    const node = graph.nodes.find((item) => item.id === anchor.stateId);
    viewportAnchorRef.current = null;
    if (!node) {
      return;
    }
    void reactFlow.setViewport({
      x: anchor.screenX - node.position.x * anchor.zoom,
      y: anchor.screenY - node.position.y * anchor.zoom,
      zoom: anchor.zoom,
    }, { duration: 0 });
  }, [graph.nodes, reactFlow]);

  function captureViewportAnchor(stateId: string) {
    const node = graph.nodes.find((item) => item.id === stateId);
    if (!node) {
      return;
    }
    const viewport = reactFlow.getViewport();
    viewportAnchorRef.current = {
      stateId,
      screenX: node.position.x * viewport.zoom + viewport.x,
      screenY: node.position.y * viewport.zoom + viewport.y,
      zoom: viewport.zoom,
    };
  }

  function clearShortcutPanTimer() {
    if (shortcutPanTimerRef.current !== null) {
      window.clearTimeout(shortcutPanTimerRef.current);
      shortcutPanTimerRef.current = null;
    }
  }

  function focusState(stateId: string, options: { selectAfterPan: boolean }) {
    const node = reactFlow.getNode(stateId) ?? graph.nodes.find((item) => item.id === stateId);
    if (!node) {
      setSelection({ type: "state", stateId });
      return;
    }

    const width = node.width ?? node.measured?.width ?? FALLBACK_NODE_WIDTH;
    const height = node.height ?? node.measured?.height ?? FALLBACK_NODE_HEIGHT;
    const currentZoom = reactFlow.getViewport().zoom;
    const zoom = Math.max(currentZoom, SHORTCUT_MIN_ZOOM);

    void reactFlow.setCenter(node.position.x + width / 2, node.position.y + height / 2, {
      zoom,
      duration: SHORTCUT_PAN_DURATION_MS,
    });

    if (!options.selectAfterPan) {
      setSelection({ type: "state", stateId });
      return;
    }

    shortcutPanTimerRef.current = window.setTimeout(() => {
      setSelection({ type: "state", stateId });
      shortcutPanTimerRef.current = null;
    }, SHORTCUT_PAN_DURATION_MS);
  }

  async function refreshFiles() {
    try {
      const payload = await fetchEditorFiles(apiBaseUrl);
      setFiles(payload.files);
      setError("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to refresh flow files");
    }
  }

  return (
    <div className="editor-shell">
      <aside className="editor-sidebar">
        <div className="editor-sidebar__header">
          <div>
            <div className="eyebrow">Flow</div>
            <div className="editor-sidebar__title">Editor</div>
          </div>
          <button className="icon-button" type="button" title="Refresh flow list" aria-label="Refresh flow list" onClick={() => void refreshFiles()}>
            ↻
          </button>
        </div>
        <input
          className="editor-search"
          value={query}
          aria-label="Search flow files"
          placeholder="Search"
          onChange={(event) => setQuery(event.target.value)}
        />
        <div className="editor-file-list nowheel">
          {filteredFiles.map((file) => (
            <button
              className={[
                "editor-file",
                document?.path === file.path ? "editor-file--active" : "",
                file.valid ? "" : "editor-file--invalid",
              ].join(" ")}
              type="button"
              data-testid="editor-flow-file"
              data-flow-name={file.name}
              data-flow-path={file.path}
              key={file.path}
              onClick={() => void openFile(file.path)}
            >
              <span className="editor-file__name">{file.name}</span>
              <span className="editor-file__meta">
                {file.stateCount} states / {file.transitionCount} decisions
              </span>
              {file.description ? <span className="editor-file__description">{file.description}</span> : null}
            </button>
          ))}
        </div>
      </aside>
      <section className="editor-main">
        <header className="editor-toolbar">
          <div className="editor-toolbar__identity" onClick={() => setSelection({ type: "flow" })}>
            <div className="editor-toolbar__title">{document?.flow.name || "No flow selected"}</div>
            <div className="editor-toolbar__path">{document?.path ?? "Start the UI server to open flow files"}</div>
          </div>
          <ValidationPills validation={document?.validation} dirty={dirty} />
          <div className="editor-toolbar__actions">
            <button className="toolbar-button" type="button" onClick={addState} disabled={!document}>
              + State
            </button>
            <button className="toolbar-button" type="button" onClick={() => reactFlow.fitView({ padding: 0.18, duration: 260 })} disabled={!document}>
              Fit
            </button>
            <button className="toolbar-button" type="button" onClick={() => void revert()} disabled={!document || busy !== null}>
              Revert
            </button>
            <button
              className="toolbar-button toolbar-button--primary"
              type="button"
              onClick={() => void save()}
              disabled={!document || busy !== null || !!document.validation.errors.length || !dirty}
            >
              Save
            </button>
          </div>
        </header>
        {notice ? <div className="editor-notice">{notice}</div> : null}
        {error ? <div className="editor-error">{error}</div> : null}
        <div className="editor-workspace">
          <div className="editor-canvas">
            {document ? (
              <ReactFlow<Node, Edge>
                key={document.path}
                data-testid="editor-flow-canvas"
                nodes={graph.nodes}
                edges={graph.edges}
                nodeTypes={editorNodeTypes}
                edgeTypes={editorEdgeTypes}
                fitView
                fitViewOptions={{ padding: 0.16 }}
                panOnScroll
                panOnScrollMode={PanOnScrollMode.Free}
                panOnDrag
                zoomOnPinch
                zoomOnScroll={false}
                onPaneClick={() => setSelection({ type: "flow" })}
                minZoom={0.18}
                maxZoom={1.8}
                nodesDraggable={false}
                nodesConnectable={false}
                edgesFocusable={false}
                edgesReconnectable={false}
                elementsSelectable
                proOptions={{ hideAttribution: true }}
              >
                <Background color="var(--canvas-grid)" gap={28} size={1.2} />
                <Controls position="bottom-left" showInteractive={false} />
              </ReactFlow>
            ) : (
              <div className="editor-empty">{busy === "loading" ? "Loading" : "No flow files found"}</div>
            )}
          </div>
          <Inspector
            document={document}
            selection={selection}
            onSelect={setSelection}
            onUpdateFlow={updateFlow}
            onUpdateState={updateState}
            onUpdateTransition={updateTransition}
            onAddTransition={addTransition}
            onDeleteTransition={deleteTransition}
            onUpdateArg={updateArg}
            onAddArg={addArg}
            onDeleteArg={deleteArg}
          />
        </div>
      </section>
    </div>
  );
}

function Inspector({
  document,
  selection,
  onSelect,
  onUpdateFlow,
  onUpdateState,
  onUpdateTransition,
  onAddTransition,
  onDeleteTransition,
  onUpdateArg,
  onAddArg,
  onDeleteArg,
}: {
  document: EditorDocument | null;
  selection: EditorSelection;
  onSelect: (selection: EditorSelection) => void;
  onUpdateFlow: (patch: Partial<EditorDocument["flow"]>) => void;
  onUpdateState: (stateId: string, patch: Partial<EditorState>) => void;
  onUpdateTransition: (stateId: string, transitionId: string, patch: Partial<EditorTransition>) => void;
  onAddTransition: (stateId: string) => void;
  onDeleteTransition: (stateId: string, transitionId: string) => void;
  onUpdateArg: (index: number, patch: Partial<EditorArg>) => void;
  onAddArg: () => void;
  onDeleteArg: (index: number) => void;
}) {
  if (!document) {
    return <aside className="editor-inspector" />;
  }
  if (selection.type === "transition") {
    const state = document.states.find((item) => item.id === selection.stateId);
    const transition = state?.transitions.find((item) => item.id === selection.transitionId);
    if (state && transition) {
      return (
        <TransitionInspector
          state={state}
          transition={transition}
          stateOptions={document.states.map((item) => item.name)}
          onBack={() => onSelect({ type: "state", stateId: state.id })}
          onUpdate={(patch) => onUpdateTransition(state.id, transition.id, patch)}
          onDelete={() => onDeleteTransition(state.id, transition.id)}
        />
      );
    }
  }
  if (selection.type === "state") {
    const state = document.states.find((item) => item.id === selection.stateId);
    if (state) {
      return (
        <StateInspector
          state={state}
          stateOptions={document.states.map((item) => item.name)}
          onUpdate={(patch) => onUpdateState(state.id, patch)}
          onTransition={(transitionId) => onSelect({ type: "transition", stateId: state.id, transitionId })}
          onUpdateTransition={(transitionId, patch) => onUpdateTransition(state.id, transitionId, patch)}
          onAddTransition={() => onAddTransition(state.id)}
          onDeleteTransition={(transitionId) => onDeleteTransition(state.id, transitionId)}
        />
      );
    }
  }
  return (
    <FlowInspector
      document={document}
      onUpdateFlow={onUpdateFlow}
      onUpdateArg={onUpdateArg}
      onAddArg={onAddArg}
      onDeleteArg={onDeleteArg}
    />
  );
}

function FlowInspector({
  document,
  onUpdateFlow,
  onUpdateArg,
  onAddArg,
  onDeleteArg,
}: {
  document: EditorDocument;
  onUpdateFlow: (patch: Partial<EditorDocument["flow"]>) => void;
  onUpdateArg: (index: number, patch: Partial<EditorArg>) => void;
  onAddArg: () => void;
  onDeleteArg: (index: number) => void;
}) {
  return (
    <aside className="editor-inspector nowheel">
      <InspectorHeader label="Flow" title={document.flow.name} />
      <Field label="Name" value={document.flow.name} onChange={(name) => onUpdateFlow({ name })} />
      <Textarea label="Description" value={document.flow.description} rows={4} onChange={(description) => onUpdateFlow({ description })} />
      <div className="field-grid">
        <Field label="Version" value={document.flow.version} onChange={(version) => onUpdateFlow({ version })} />
        <SelectField label="Mode" value={document.flow.mode} options={MODE_OPTIONS} onChange={(mode) => onUpdateFlow({ mode })} />
      </div>
      <div className="field-grid">
        <SelectField label="Thinking" value={document.flow.thinking} options={THINKING_OPTIONS} onChange={(thinking) => onUpdateFlow({ thinking })} />
        <FastField value={document.flow.fast} onChange={(fast) => onUpdateFlow({ fast })} />
      </div>
      <Field label="Working dir" value={document.flow.path} onChange={(path) => onUpdateFlow({ path })} />
      <div className="inspector-section-title">
        <span>Args</span>
        <button className="icon-button" type="button" title="Add arg" aria-label="Add arg" onClick={onAddArg}>
          +
        </button>
      </div>
      {document.flow.args.map((arg, index) => (
        <div className="arg-row" key={`${arg.name}-${index}`}>
          <Field label="Arg" value={arg.name} onChange={(name) => onUpdateArg(index, { name })} />
          <Field label="Default" value={arg.default} onChange={(value) => onUpdateArg(index, { default: value })} />
          <Textarea label="Help" value={arg.help} rows={2} onChange={(help) => onUpdateArg(index, { help })} />
          <button className="subtle-danger" type="button" onClick={() => onDeleteArg(index)}>
            Delete arg
          </button>
        </div>
      ))}
      <ValidationList validation={document.validation} />
    </aside>
  );
}

function StateInspector({
  state,
  stateOptions,
  onUpdate,
  onTransition,
  onUpdateTransition,
  onAddTransition,
  onDeleteTransition,
}: {
  state: EditorState;
  stateOptions: string[];
  onUpdate: (patch: Partial<EditorState>) => void;
  onTransition: (transitionId: string) => void;
  onUpdateTransition: (transitionId: string, patch: Partial<EditorTransition>) => void;
  onAddTransition: () => void;
  onDeleteTransition: (transitionId: string) => void;
}) {
  return (
    <aside className="editor-inspector nowheel">
      <InspectorHeader label="State" title={state.name} />
      <Field label="Name" value={state.name} onChange={(name) => onUpdate({ name })} />
      <div className="toggle-row">
        <Toggle label="Start" checked={state.start} onChange={(start) => onUpdate({ start })} />
        <Toggle label="End" checked={state.end} onChange={(end) => onUpdate({ end })} />
      </div>
      <div className="field-grid">
        <Field label="Wait" value={state.wait} onChange={(wait) => onUpdate({ wait })} />
        <SelectField label="Thinking" value={state.thinking} options={THINKING_OPTIONS} onChange={(thinking) => onUpdate({ thinking })} />
      </div>
      <div className="field-grid">
        <SelectField label="Mode" value={state.mode} options={MODE_OPTIONS} onChange={(mode) => onUpdate({ mode })} />
        <FastField value={state.fast} onChange={(fast) => onUpdate({ fast })} />
      </div>
      <Textarea label="Prompt" value={state.prompt} rows={12} onChange={(prompt) => onUpdate({ prompt })} />
      <div className="inspector-section-title">
        <span>Decisions</span>
        <button className="icon-button" type="button" title="Add decision" aria-label="Add decision" onClick={onAddTransition}>
          +
        </button>
      </div>
      {state.transitions.map((transition) => (
        <button className="decision-row" type="button" key={transition.id} onClick={() => onTransition(transition.id)}>
          <span>{transition.condition || "otherwise"}</span>
          <strong>{transition.target || "target"}</strong>
        </button>
      ))}
      {state.transitions.map((transition) => (
        <div className="compact-transition" key={`${transition.id}-inline`}>
          <Textarea
            label="Decision"
            value={transition.condition}
            rows={2}
            onChange={(condition) => onUpdateTransition(transition.id, { condition })}
          />
          <div className="field-grid">
            <SelectField
              label="Target"
              value={transition.target}
              options={stateOptions}
              onChange={(target) => onUpdateTransition(transition.id, { target })}
            />
            <Field label="Wait" value={transition.wait} onChange={(wait) => onUpdateTransition(transition.id, { wait })} />
          </div>
          <button className="subtle-danger" type="button" onClick={() => onDeleteTransition(transition.id)}>
            Delete decision
          </button>
        </div>
      ))}
    </aside>
  );
}

function TransitionInspector({
  state,
  transition,
  stateOptions,
  onBack,
  onUpdate,
  onDelete,
}: {
  state: EditorState;
  transition: EditorTransition;
  stateOptions: string[];
  onBack: () => void;
  onUpdate: (patch: Partial<EditorTransition>) => void;
  onDelete: () => void;
}) {
  return (
    <aside className="editor-inspector nowheel">
      <button className="ghost-button inspector-back" type="button" onClick={onBack}>
        Back to {state.name}
      </button>
      <InspectorHeader label="Decision" title={transition.condition || "otherwise"} />
      <Textarea label="Decision" value={transition.condition} rows={7} onChange={(condition) => onUpdate({ condition })} />
      <SelectField label="Target" value={transition.target} options={stateOptions} onChange={(target) => onUpdate({ target })} />
      <Field label="Wait" value={transition.wait} onChange={(wait) => onUpdate({ wait })} />
      <button className="subtle-danger" type="button" onClick={onDelete}>
        Delete decision
      </button>
    </aside>
  );
}

function InspectorHeader({ label, title }: { label: string; title: string }) {
  return (
    <div className="inspector-header">
      <div className="eyebrow">{label}</div>
      <h2>{title}</h2>
    </div>
  );
}

function Field({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return (
    <label className="editor-field">
      <span>{label}</span>
      <input className="nodrag nopan" value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function Textarea({
  label,
  value,
  rows,
  onChange,
}: {
  label: string;
  value: string;
  rows: number;
  onChange: (value: string) => void;
}) {
  return (
    <label className="editor-field">
      <span>{label}</span>
      <textarea className="nodrag nopan nowheel" value={value} rows={rows} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function SelectField({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="editor-field">
      <span>{label}</span>
      <select className="nodrag nopan" value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => (
          <option key={option || "inherit"} value={option}>
            {option || "default"}
          </option>
        ))}
      </select>
    </label>
  );
}

function FastField({ value, onChange }: { value: boolean | null; onChange: (value: boolean | null) => void }) {
  return (
    <label className="editor-field">
      <span>Fast</span>
      <select
        className="nodrag nopan"
        value={value === null ? "" : value ? "true" : "false"}
        onChange={(event) => onChange(event.target.value === "" ? null : event.target.value === "true")}
      >
        <option value="">default</option>
        <option value="true">on</option>
        <option value="false">off</option>
      </select>
    </label>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <label className="editor-toggle">
      <input className="nodrag nopan" type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

function ValidationPills({ validation, dirty }: { validation?: EditorValidation; dirty: boolean }) {
  if (!validation) {
    return null;
  }
  return (
    <div className="editor-validation-pills">
      {dirty ? <span className="summary-pill summary-pill--info">edited</span> : <span className="summary-pill summary-pill--ok">saved</span>}
      {validation.errors.length ? (
        <span className="summary-pill summary-pill--error">{validation.errors.length} errors</span>
      ) : (
        <span className="summary-pill summary-pill--ok">valid</span>
      )}
      {validation.warnings.length ? <span className="summary-pill summary-pill--warn">{validation.warnings.length} warnings</span> : null}
    </div>
  );
}

function ValidationList({ validation }: { validation: EditorValidation }) {
  const items = [
    ...validation.errors.map((message) => ({ tone: "error", message })),
    ...validation.warnings.map((message) => ({ tone: "warning", message })),
  ];
  if (!items.length) {
    return null;
  }
  return (
    <div className="validation-list">
      {items.slice(0, 8).map((item, index) => (
        <div className={`validation-list__item validation-list__item--${item.tone}`} key={`${item.tone}-${index}`}>
          {item.message}
        </div>
      ))}
    </div>
  );
}

function selectionExists(document: EditorDocument, selection: EditorSelection): boolean {
  if (selection.type === "flow") {
    return true;
  }
  const state = document.states.find((item) => item.id === selection.stateId);
  if (!state) {
    return false;
  }
  if (selection.type === "transition") {
    return state.transitions.some((item) => item.id === selection.transitionId);
  }
  return true;
}

function uniqueStateName(states: EditorState[], base: string): string {
  const names = new Set(states.map((state) => state.name));
  if (!names.has(base)) {
    return base;
  }
  let index = 2;
  while (names.has(`${base}-${index}`)) {
    index += 1;
  }
  return `${base}-${index}`;
}

function uniqueArgName(args: EditorArg[]): string {
  const names = new Set(args.map((arg) => arg.name));
  if (!names.has("arg")) {
    return "arg";
  }
  let index = 2;
  while (names.has(`arg${index}`)) {
    index += 1;
  }
  return `arg${index}`;
}

function makeId(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function isEditableShortcutTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  return Boolean(target.closest("input, textarea, select, [contenteditable='true']"));
}
