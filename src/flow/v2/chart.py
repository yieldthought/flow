"""Standalone HTML flow-chart rendering backed by Graphviz."""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path

from .spec import FlowSpec, StateSpec, TransitionSpec


class ChartRenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChartPalette:
    color_scheme: str
    background: str
    surface: str
    surface_raised: str
    header: str
    canvas: str
    line: str
    text: str
    muted: str
    subtle: str
    prompt: str
    cyan: str
    green: str
    coral: str
    yellow: str
    violet: str
    node_fill: str
    node_stroke: str
    start_fill: str
    start_stroke: str
    success_fill: str
    success_stroke: str
    failure_fill: str
    failure_stroke: str
    edge: str
    edge_text: str
    wait_edge: str
    wait_text: str


PALETTES = {
    "dark": ChartPalette(
        color_scheme="dark",
        background="#101317",
        surface="#171b20",
        surface_raised="#1c2229",
        header="#13171c",
        canvas="#12161b",
        line="#313942",
        text="#edf2f5",
        muted="#9aa6af",
        subtle="#6f7b85",
        prompt="#c8d0d6",
        cyan="#8bd5e8",
        green="#9bd6ad",
        coral="#f0a0a8",
        yellow="#e8d18a",
        violet="#c6b5eb",
        node_fill="#1b2027",
        node_stroke="#56616d",
        start_fill="#19272d",
        start_stroke="#79c4d5",
        success_fill="#17271f",
        success_stroke="#77bd8d",
        failure_fill="#2b1d21",
        failure_stroke="#dc828c",
        edge="#778592",
        edge_text="#aeb8c1",
        wait_edge="#cbb66f",
        wait_text="#dfca82",
    ),
    "light": ChartPalette(
        color_scheme="light",
        background="#f6f8fa",
        surface="#ffffff",
        surface_raised="#eef2f5",
        header="#fbfcfd",
        canvas="#f3f6f8",
        line="#d5dce2",
        text="#18232c",
        muted="#52616d",
        subtle="#74818b",
        prompt="#34424d",
        cyan="#43899a",
        green="#4e9363",
        coral="#bd636d",
        yellow="#8d7429",
        violet="#7563a0",
        node_fill="#ffffff",
        node_stroke="#8b98a2",
        start_fill="#e3f3f6",
        start_stroke="#5799a8",
        success_fill="#e7f4ea",
        success_stroke="#65a278",
        failure_fill="#fae9eb",
        failure_stroke="#c6747d",
        edge="#778692",
        edge_text="#52616d",
        wait_edge="#a58b38",
        wait_text="#7f6824",
    ),
}


def write_chart(flow: FlowSpec, output: str | Path | None = None, *, theme: str = "dark") -> Path:
    svg = render_svg(flow, theme=theme)
    document = render_html(flow, svg, theme=theme)
    if output is not None:
        path = Path(output).expanduser().resolve()
        path.write_text(document, encoding="utf-8")
        return path

    slug = re.sub(r"[^a-z0-9]+", "-", flow.name.lower()).strip("-") or "flow"
    descriptor, filename = tempfile.mkstemp(prefix=f"flow-chart-{slug}-", suffix=".html")
    path = Path(filename)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(document)
    return path


def render_svg(flow: FlowSpec, *, executable: str | None = None, theme: str = "dark") -> str:
    dot = executable or shutil.which("dot")
    if not dot:
        raise ChartRenderError("Graphviz 'dot' was not found on PATH; install Graphviz to render flow charts")
    try:
        result = subprocess.run(
            [dot, "-Tsvg"],
            input=render_dot(flow, theme=theme),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise ChartRenderError("Graphviz did not finish rendering within 30 seconds") from exc
    except OSError as exc:
        raise ChartRenderError(f"could not run Graphviz: {exc}") from exc
    if result.returncode != 0:
        detail = " ".join(result.stderr.split()) or f"dot exited {result.returncode}"
        raise ChartRenderError(f"Graphviz could not render the flow: {detail}")
    start = result.stdout.find("<svg")
    if start < 0:
        raise ChartRenderError("Graphviz returned no SVG document")
    return result.stdout[start:]


def render_dot(flow: FlowSpec, *, theme: str = "dark") -> str:
    palette = _palette(theme)
    state_ids = {name: f"n{index}" for index, name in enumerate(flow.states)}
    lines = [
        "digraph flow {",
        "  graph [bgcolor=\"transparent\", rankdir=TB, pad=\"0.35\", nodesep=\"0.32\", "
        "ranksep=\"0.82\", splines=spline, outputorder=edgesfirst];",
        "  node [shape=box, style=\"rounded,filled\", fontname=\"Helvetica\", fontsize=12, "
        f"margin=\"0.18,0.12\", color=\"{palette.node_stroke}\", fillcolor=\"{palette.node_fill}\", "
        f"fontcolor=\"{palette.text}\", penwidth=1.4];",
        f"  edge [fontname=\"Helvetica\", fontsize=10, color=\"{palette.edge}\", "
        f"fontcolor=\"{palette.edge_text}\", arrowsize=0.72, penwidth=1.2];",
        "  __start [shape=circle, label=\"\", width=0.18, fixedsize=true, "
        f"style=filled, fillcolor=\"{palette.cyan}\", color=\"{palette.cyan}\"] ;",
        f"  __start -> {state_ids[flow.start_state]} [id=\"start-edge\", "
        f"color=\"{palette.cyan}\", penwidth=1.8];",
    ]
    for state in flow.states.values():
        lines.append(_dot_state(state, state_ids[state.name], palette))
    terminal_anchors: dict[str, tuple[str, int]] = {}
    for state in flow.states.values():
        for transition_index, transition in enumerate(state.transitions):
            if flow.states[transition.target].terminal:
                terminal_anchors[transition.target] = (state.name, transition_index)
    edge_index = 0
    for state in flow.states.values():
        for transition_index, transition in enumerate(state.transitions):
            anchor = terminal_anchors.get(transition.target)
            lines.append(
                _dot_transition(
                    state_ids[state.name],
                    state_ids[transition.target],
                    transition,
                    palette,
                    edge_id=f"e{edge_index}",
                    constraint=anchor is None or anchor == (state.name, transition_index),
                )
            )
            edge_index += 1
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_html(flow: FlowSpec, svg: str, *, theme: str = "dark") -> str:
    palette = _palette(theme)
    data = _safe_json(_flow_payload(flow))
    title = html.escape(flow.name)
    description = html.escape(flow.description or "No description provided.")
    source = html.escape(flow.source_path)
    transitions = sum(len(state.transitions) for state in flow.states.values())
    terminal = sum(state.terminal for state in flow.states.values())
    model = html.escape(flow.model or "default model")
    return f"""<!doctype html>
<html lang="en" data-theme="{theme}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - Flow chart</title>
<style>
:root {{
  color-scheme: {palette.color_scheme};
  --bg: {palette.background};
  --surface: {palette.surface};
  --surface-raised: {palette.surface_raised};
  --header: {palette.header};
  --canvas: {palette.canvas};
  --line: {palette.line};
  --text: {palette.text};
  --muted: {palette.muted};
  --subtle: {palette.subtle};
  --prompt: {palette.prompt};
  --cyan: {palette.cyan};
  --green: {palette.green};
  --coral: {palette.coral};
  --yellow: {palette.yellow};
  --violet: {palette.violet};
  --viewport-width: 100vw;
}}
* {{ box-sizing: border-box; }}
html, body {{ min-height: 100%; }}
body {{
  margin: 0;
  min-width: min-content;
  background: var(--bg);
  color: var(--text);
  font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  letter-spacing: 0;
}}
button {{ font: inherit; }}
.page-header {{
  position: sticky;
  left: 0;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 32px;
  width: var(--viewport-width);
  padding: 24px 28px 20px;
  border-bottom: 1px solid var(--line);
  background: var(--header);
}}
.identity {{ min-width: 0; max-width: 840px; }}
.eyebrow {{
  margin: 0 0 7px;
  color: var(--cyan);
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
}}
h1 {{ margin: 0; font-size: 26px; line-height: 1.15; letter-spacing: 0; }}
.description {{ margin: 9px 0 0; color: var(--muted); line-height: 1.45; }}
.source {{
  margin: 8px 0 0;
  overflow: hidden;
  color: var(--subtle);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.metrics {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 18px; margin: 2px 0 0; }}
.metric {{ min-width: 56px; }}
.metric dt {{ color: var(--subtle); font-size: 10px; text-transform: uppercase; }}
.metric dd {{ margin: 3px 0 0; color: var(--text); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
.workspace {{ min-width: min-content; }}
.graph-pane {{ min-width: 0; border-bottom: 1px solid var(--line); }}
.graph-toolbar {{
  position: sticky;
  left: 0;
  display: flex;
  align-items: center;
  gap: 18px;
  width: var(--viewport-width);
  min-height: 45px;
  padding: 10px 28px;
  border-bottom: 1px solid var(--line);
  color: var(--muted);
}}
.zoom-controls {{ display: flex; align-items: center; gap: 6px; flex: 0 0 auto; }}
.zoom-controls button {{
  height: 30px;
  min-width: 30px;
  padding: 0 8px;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--surface-raised);
  color: var(--text);
  cursor: pointer;
}}
.zoom-controls button:hover {{ border-color: var(--cyan); color: var(--cyan); }}
.zoom-controls .fit-width {{ min-width: 72px; }}
.zoom-value {{
  min-width: 42px;
  margin-left: 3px;
  color: var(--subtle);
  font: 11px ui-monospace, SFMono-Regular, Menlo, monospace;
}}
.legend {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 12px; margin-left: auto; font-size: 11px; }}
.legend span {{ display: inline-flex; align-items: center; gap: 5px; }}
.dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }}
.dot.start {{ background: var(--cyan); }}
.dot.success {{ background: var(--green); }}
.dot.failure {{ background: var(--coral); }}
.dot.wait {{ background: var(--yellow); }}
.graph-canvas {{
  padding: 30px 28px;
  background-color: var(--canvas);
}}
.stage {{ width: max-content; min-width: 100%; }}
.stage svg {{ display: block; height: auto; max-width: none; margin: 0 auto; }}
.stage .node {{ cursor: pointer; }}
.stage.overview .node path, .stage.overview .node polygon {{
  stroke: var(--node-color, var(--muted)) !important;
  stroke-width: 2px !important;
}}
.stage.overview .edge path {{
  stroke: var(--destination-color, var(--muted)) !important;
  stroke-width: 1.7px !important;
}}
.stage.overview .edge polygon {{
  stroke: var(--destination-color, var(--muted)) !important;
  fill: var(--destination-color, var(--muted)) !important;
}}
.stage:not(.overview) .node path, .stage:not(.overview) .node polygon {{
  stroke: var(--subtle) !important;
  stroke-width: 1.4px !important;
}}
.stage:not(.overview) .edge path {{
  stroke: var(--subtle) !important;
  stroke-width: 1.2px !important;
}}
.stage:not(.overview) .edge polygon {{
  stroke: var(--subtle) !important;
  fill: var(--subtle) !important;
}}
.stage:not(.overview) .edge text {{ fill: var(--subtle) !important; }}
.stage .node.inbound-node path, .stage .node.inbound-node polygon {{
  stroke: var(--node-color, var(--muted)) !important;
  stroke-width: 2.2px !important;
}}
.stage .node.outbound-node path, .stage .node.outbound-node polygon {{
  stroke: var(--node-color, var(--muted)) !important;
  stroke-width: 2.2px !important;
}}
.stage .edge.inbound-edge path {{
  stroke: var(--destination-color, var(--muted)) !important;
  stroke-width: 2.2px !important;
}}
.stage .edge.inbound-edge polygon {{
  stroke: var(--destination-color, var(--muted)) !important;
  fill: var(--destination-color, var(--muted)) !important;
}}
.stage .edge.inbound-edge text {{
  fill: var(--destination-color, var(--muted)) !important;
  font-weight: 600;
}}
.stage .edge.outbound-edge path {{
  stroke: var(--destination-color, var(--muted)) !important;
  stroke-width: 2.2px !important;
}}
.stage .edge.outbound-edge polygon {{
  stroke: var(--destination-color, var(--muted)) !important;
  fill: var(--destination-color, var(--muted)) !important;
}}
.stage .edge.outbound-edge text {{
  fill: var(--destination-color, var(--muted)) !important;
  font-weight: 600;
}}
.stage .node:hover path, .stage .node:hover polygon {{
  stroke: var(--node-color, var(--text)) !important;
  stroke-width: 2.4px !important;
}}
.stage .node.selected path, .stage .node.selected polygon {{
  stroke: var(--node-color, var(--text)) !important;
  stroke-width: 2.8px !important;
  filter: none !important;
}}
.inspector {{
  position: sticky;
  left: 0;
  width: var(--viewport-width);
  padding: 24px 28px 34px;
  background: var(--surface);
}}
.inspector-header {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; }}
.inspector h2 {{ margin: 0; font-size: 19px; line-height: 1.25; overflow-wrap: anywhere; }}
.badges {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; min-height: 25px; }}
.badge {{
  display: inline-flex;
  align-items: center;
  min-height: 23px;
  padding: 3px 7px;
  border: 1px solid var(--line);
  border-radius: 4px;
  color: var(--muted);
  font: 11px ui-monospace, SFMono-Regular, Menlo, monospace;
}}
.badge.start {{ border-color: var(--cyan); color: var(--cyan); }}
.badge.success {{ border-color: var(--green); color: var(--green); }}
.badge.failure {{ border-color: var(--coral); color: var(--coral); }}
.badge.wait {{ border-color: var(--yellow); color: var(--yellow); }}
.inspector-grid {{
  display: grid;
  grid-template-columns: minmax(0, 3fr) minmax(300px, 2fr);
  gap: 40px;
  margin-top: 18px;
}}
.section {{ margin: 0; padding-top: 18px; border-top: 1px solid var(--line); }}
.section h3 {{ margin: 0 0 9px; color: var(--subtle); font-size: 10px; text-transform: uppercase; }}
.prompt {{ margin: 0; color: var(--prompt); font-size: 12px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }}
.transitions {{ display: grid; gap: 12px; margin: 0; padding: 0; list-style: none; }}
.transition-target {{
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--target-color, var(--violet));
  cursor: pointer;
  font: 12px ui-monospace, SFMono-Regular, Menlo, monospace;
}}
.transition-target:hover {{ color: var(--text); }}
.transition-condition {{ margin-top: 4px; color: var(--muted); font-size: 12px; line-height: 1.4; overflow-wrap: anywhere; }}
.transition-wait {{ margin-top: 4px; color: var(--yellow); font-size: 11px; }}
.empty {{ color: var(--subtle); font-style: italic; }}
@media (max-width: 880px) {{
  .page-header {{ display: block; padding: 20px; }}
  .metrics {{ justify-content: flex-start; margin-top: 20px; }}
  .graph-toolbar {{ align-items: flex-start; flex-wrap: wrap; padding: 10px 20px; }}
  .legend {{ justify-content: flex-start; width: 100%; margin-left: 0; }}
  .graph-canvas {{ padding: 22px 20px; }}
  .inspector {{ padding: 22px 20px 30px; }}
  .inspector-header {{ display: block; }}
  .badges {{ justify-content: flex-start; margin-top: 10px; }}
  .inspector-grid {{ display: block; }}
  .section + .section {{ margin-top: 26px; }}
}}
</style>
</head>
<body>
<header class="page-header">
  <div class="identity">
    <p class="eyebrow">Flow chart</p>
    <h1>{title}</h1>
    <p class="description">{description}</p>
    <p class="source" title="{source}">{source}</p>
  </div>
  <dl class="metrics">
    <div class="metric"><dt>States</dt><dd>{len(flow.states)}</dd></div>
    <div class="metric"><dt>Transitions</dt><dd>{transitions}</dd></div>
    <div class="metric"><dt>Terminals</dt><dd>{terminal}</dd></div>
    <div class="metric"><dt>Mode</dt><dd>{html.escape(flow.mode)}</dd></div>
    <div class="metric"><dt>Thinking</dt><dd>{html.escape(flow.thinking)}</dd></div>
    <div class="metric"><dt>Model</dt><dd>{model}</dd></div>
  </dl>
</header>
<main class="workspace">
  <section class="graph-pane" aria-label="Flow graph">
    <div class="graph-toolbar">
      <div class="zoom-controls" aria-label="Graph zoom">
        <button id="zoom-out" type="button" title="Zoom graph out" aria-label="Zoom graph out">&minus;</button>
        <button id="zoom-in" type="button" title="Zoom graph in" aria-label="Zoom graph in">+</button>
        <button id="fit-width" class="fit-width" type="button" title="Fit graph to browser width">Fit width</button>
        <span id="zoom-value" class="zoom-value">100%</span>
      </div>
      <div class="legend" aria-label="Legend">
        <span><i class="dot start"></i>start</span>
        <span><i class="dot wait"></i>wait</span>
        <span><i class="dot success"></i>exit 0</span>
        <span><i class="dot failure"></i>nonzero exit</span>
      </div>
    </div>
    <div class="graph-canvas">
      <div id="stage" class="stage">{svg}</div>
    </div>
  </section>
  <aside class="inspector" aria-live="polite" hidden>
    <div class="inspector-header">
      <h2 id="state-name"></h2>
      <div id="badges" class="badges"></div>
    </div>
    <div class="inspector-grid">
      <section class="section">
        <h3>Prompt</h3>
        <p id="prompt" class="prompt"></p>
      </section>
      <section class="section">
        <h3>Transitions</h3>
        <ul id="transitions" class="transitions"></ul>
      </section>
    </div>
  </aside>
</main>
<script id="flow-data" type="application/json">{data}</script>
<script>
(() => {{
  const flow = JSON.parse(document.getElementById('flow-data').textContent);
  const svg = document.querySelector('#stage svg');
  const stage = document.getElementById('stage');
  const graphCanvas = document.querySelector('.graph-canvas');
  const inspector = document.querySelector('.inspector');
  const zoomValue = document.getElementById('zoom-value');
  const intrinsicWidth = Math.max(1, svg.getBoundingClientRect().width);
  const defaultWidth = intrinsicWidth * 0.8;
  const stateByName = new Map(flow.states.map(state => [state.name, state]));
  let scale = 1;
  let fitWidthActive = false;

  function syncViewportWidth() {{
    document.documentElement.style.setProperty(
      '--viewport-width', `${{document.documentElement.clientWidth}}px`
    );
  }}

  function setScale(next) {{
    scale = Math.min(4, Math.max(0.2, next));
    svg.style.width = `${{Math.round(defaultWidth * scale)}}px`;
    zoomValue.textContent = `${{Math.round(scale * 100)}}%`;
  }}

  function fitWidth() {{
    const style = getComputedStyle(graphCanvas);
    const padding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    const available = Math.max(1, document.documentElement.clientWidth - padding);
    fitWidthActive = true;
    setScale(Math.min(1, available / defaultWidth));
    requestAnimationFrame(() => window.scrollTo({{ left: 0, top: window.scrollY }}));
  }}

  function zoomBy(factor) {{
    fitWidthActive = false;
    setScale(scale * factor);
  }}

  function hashName(value) {{
    let hash = 2166136261;
    for (let index = 0; index < value.length; index += 1) {{
      hash ^= value.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }}
    return hash >>> 0;
  }}

  function stateColor(name) {{
    const hue = hashName(name) % 360;
    const dark = document.documentElement.dataset.theme === 'dark';
    return `hsl(${{hue}} ${{dark ? 58 : 50}}% ${{dark ? 72 : 44}}%)`;
  }}

  function applyOverviewColors() {{
    const colors = new Map();
    for (const state of flow.states) {{
      const color = stateColor(state.name);
      colors.set(state.name, color);
      document.getElementById(`state-${{state.id}}`)?.style.setProperty('--node-color', color);
    }}
    for (const source of flow.states) {{
      for (const transition of source.transitions) {{
        document.getElementById(`edge-${{transition.id}}`)?.style.setProperty(
          '--destination-color', colors.get(transition.target)
        );
      }}
    }}
    document.getElementById('start-edge')?.style.setProperty(
      '--destination-color', colors.get(flow.start)
    );
  }}

  function setSvgTitle(element, text) {{
    const title = element?.querySelector('title');
    if (title) title.textContent = text;
    element?.setAttribute('aria-label', text);
  }}

  function applyTooltips() {{
    setSvgTitle(document.getElementById('start-edge'), `start -> ${{flow.start}}`);
    for (const state of flow.states) {{
      setSvgTitle(document.getElementById(`state-${{state.id}}`), state.name);
      for (const transition of state.transitions) {{
        const condition = transition.condition || 'always';
        const label = transition.wait ? `${{condition}}\nwait ${{transition.wait}}` : condition;
        setSvgTitle(document.getElementById(`edge-${{transition.id}}`), label);
      }}
    }}
  }}

  function clearFocusedGraph() {{
    document.querySelectorAll('.stage .node').forEach(node =>
      node.classList.remove('selected', 'inbound-node', 'outbound-node')
    );
    document.querySelectorAll('.stage .edge').forEach(edge =>
      edge.classList.remove('inbound-edge', 'outbound-edge')
    );
  }}

  function showOverview() {{
    clearFocusedGraph();
    stage.classList.add('overview');
    inspector.hidden = true;
  }}

  function badge(text, className = '') {{
    const item = document.createElement('span');
    item.className = `badge ${{className}}`;
    item.textContent = text;
    return item;
  }}

  function revealState(state) {{
    const node = document.getElementById(`state-${{state.id}}`);
    if (!node) return;
    node.scrollIntoView({{ behavior: 'smooth', block: 'center', inline: 'center' }});
  }}

  function centerStateHorizontally(state) {{
    const node = document.getElementById(`state-${{state.id}}`);
    if (!node) return;
    const nodeBox = node.getBoundingClientRect();
    const left = window.scrollX + nodeBox.left - (window.innerWidth - nodeBox.width) / 2;
    window.scrollTo({{ left: Math.max(0, left), top: 0 }});
  }}

  function selectState(state) {{
    clearFocusedGraph();
    stage.classList.remove('overview');
    inspector.hidden = false;
    if (state.start) document.getElementById('start-edge')?.classList.add('inbound-edge');
    for (const source of flow.states) {{
      for (const transition of source.transitions) {{
        const edge = document.getElementById(`edge-${{transition.id}}`);
        if (transition.target === state.name) {{
          edge?.classList.add('inbound-edge');
          document.getElementById(`state-${{source.id}}`)?.classList.add('inbound-node');
        }}
        if (source.name === state.name) {{
          edge?.classList.add('outbound-edge');
          const target = stateByName.get(transition.target);
          if (target) document.getElementById(`state-${{target.id}}`)?.classList.add('outbound-node');
        }}
      }}
    }}
    document.getElementById(`state-${{state.id}}`)?.classList.add('selected');
    document.getElementById('state-name').textContent = state.name;
    const badges = document.getElementById('badges');
    badges.replaceChildren();
    if (state.start) badges.append(badge('start', 'start'));
    if (state.terminal) badges.append(badge(`exit ${{state.exit_code}}`, state.exit_code === 0 ? 'success' : 'failure'));
    if (state.wait) badges.append(badge(`wait ${{state.wait}}`, 'wait'));
    if (state.mode) badges.append(badge(`mode ${{state.mode}}`));
    if (state.thinking) badges.append(badge(`thinking ${{state.thinking}}`));
    if (state.fast !== null) badges.append(badge(state.fast ? 'fast' : 'not fast'));

    const prompt = document.getElementById('prompt');
    prompt.textContent = state.prompt || 'No prompt.';
    prompt.classList.toggle('empty', !state.prompt);

    const list = document.getElementById('transitions');
    list.replaceChildren();
    if (!state.transitions.length) {{
      const empty = document.createElement('li');
      empty.className = 'empty';
      empty.textContent = 'Terminal state.';
      list.append(empty);
    }}
    for (const transition of state.transitions) {{
      const item = document.createElement('li');
      const target = document.createElement('button');
      target.type = 'button';
      target.className = 'transition-target';
      target.textContent = `to ${{transition.target}}`;
      target.title = `Show ${{transition.target}}`;
      target.style.setProperty('--target-color', stateColor(transition.target));
      target.addEventListener('click', () => {{
        const next = flow.states.find(candidate => candidate.name === transition.target);
        if (next) {{
          selectState(next);
          revealState(next);
        }}
      }});
      const condition = document.createElement('div');
      condition.className = 'transition-condition';
      condition.textContent = transition.condition || 'Always';
      item.append(target, condition);
      if (transition.wait) {{
        const wait = document.createElement('div');
        wait.className = 'transition-wait';
        wait.textContent = `wait ${{transition.wait}}`;
        item.append(wait);
      }}
      list.append(item);
    }}
  }}

  document.getElementById('zoom-in').addEventListener('click', () => zoomBy(1.2));
  document.getElementById('zoom-out').addEventListener('click', () => zoomBy(1 / 1.2));
  document.getElementById('fit-width').addEventListener('click', fitWidth);
  graphCanvas.addEventListener('click', event => {{
    if (!(event.target instanceof Element) || !event.target.closest('.node')) showOverview();
  }});
  for (const state of flow.states) {{
    const node = document.getElementById(`state-${{state.id}}`);
    if (node) node.addEventListener('click', () => selectState(state));
  }}
  window.addEventListener('resize', () => {{
    syncViewportWidth();
    if (fitWidthActive) fitWidth();
  }});
  const initialState = flow.states.find(state => state.name === flow.start) || flow.states[0];
  syncViewportWidth();
  setScale(1);
  applyOverviewColors();
  applyTooltips();
  showOverview();
  requestAnimationFrame(() => centerStateHorizontally(initialState));
}})();
</script>
</body>
</html>
"""


def _dot_state(state: StateSpec, state_id: str, palette: ChartPalette) -> str:
    if state.terminal:
        success = state.exit_code == 0
        fill = palette.success_fill if success else palette.failure_fill
        stroke = palette.success_stroke if success else palette.failure_stroke
    elif state.start:
        fill, stroke = palette.start_fill, palette.start_stroke
    else:
        fill, stroke = palette.node_fill, palette.node_stroke
    return (
        f"  {state_id} [id=\"state-{state_id}\", label={_state_label(state, palette)}, "
        f"fillcolor=\"{fill}\", color=\"{stroke}\", tooltip={_dot_quote(_state_tooltip(state))}];"
    )


def _state_label(state: StateSpec, palette: ChartPalette) -> str:
    notes: list[str] = []
    if state.start:
        notes.append("START")
    if state.wait:
        notes.append(f"WAIT {state.wait}")
    if state.terminal:
        notes.append(f"EXIT {state.exit_code}")
    name = html.escape(state.name)
    rows = [f'<TR><TD><FONT POINT-SIZE="14"><B>{name}</B></FONT></TD></TR>']
    if notes:
        note = html.escape("  /  ".join(notes))
        rows.append(
            f'<TR><TD><FONT POINT-SIZE="9" COLOR="{palette.edge_text}">{note}</FONT></TD></TR>'
        )
    return "<<TABLE BORDER=\"0\" CELLBORDER=\"0\" CELLPADDING=\"2\">" + "".join(rows) + "</TABLE>>"


def _dot_transition(
    source: str,
    target: str,
    transition: TransitionSpec,
    palette: ChartPalette,
    *,
    edge_id: str,
    constraint: bool,
) -> str:
    label = transition.condition or "always"
    if transition.wait:
        label = f"{label}\nwait {transition.wait}"
    wrapped = _wrapped_label(label)
    attributes = [
        f'id="edge-{edge_id}"',
        f"label={_dot_quote(wrapped)}",
        f"tooltip={_dot_quote(label)}",
    ]
    if not constraint:
        attributes.append("constraint=false")
    if transition.wait:
        attributes.extend(
            [
                f'color="{palette.wait_edge}"',
                f'fontcolor="{palette.wait_text}"',
                'style="dashed"',
            ]
        )
    return f"  {source} -> {target} [{', '.join(attributes)}];"


def _wrapped_label(value: str) -> str:
    width = 19
    lines: list[str] = []
    for paragraph in value.splitlines():
        lines.extend(textwrap.wrap(" ".join(paragraph.split()), width=width) or [""])
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = lines[-1][: width - 3].rstrip() + "..."
    return "\n".join(lines)


def _state_tooltip(state: StateSpec) -> str:
    if state.terminal:
        return f"{state.name}: exit {state.exit_code}"
    if state.prompt.strip():
        summary = " ".join(state.prompt.split())
        return f"{state.name}: {summary[:220]}"
    return state.name


def _dot_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _flow_payload(flow: FlowSpec) -> dict[str, object]:
    edge_index = 0
    states: list[dict[str, object]] = []
    for state_index, state in enumerate(flow.states.values()):
        transitions = []
        for item in state.transitions:
            transitions.append(
                {
                    "id": f"e{edge_index}",
                    "target": item.target,
                    "condition": item.condition,
                    "wait": item.wait,
                }
            )
            edge_index += 1
        states.append(_state_payload(state, state_index, transitions))
    return {"start": flow.start_state, "states": states}


def _state_payload(
    state: StateSpec,
    index: int,
    transitions: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "id": f"n{index}",
        "name": state.name,
        "start": state.start,
        "terminal": state.terminal,
        "exit_code": state.exit_code,
        "prompt": state.prompt,
        "wait": state.wait,
        "mode": state.mode,
        "thinking": state.thinking,
        "fast": state.fast,
        "transitions": transitions,
    }


def _safe_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _palette(theme: str) -> ChartPalette:
    try:
        return PALETTES[theme]
    except KeyError as exc:
        raise ValueError(f"unknown chart theme: {theme}") from exc
