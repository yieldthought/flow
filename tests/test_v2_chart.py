from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import replace

import pytest

from flow.v2.chart import ChartRenderError, render_dot, render_html, render_svg
from flow.v2.spec import FlowSpec, flow_from_mapping


def sample_flow() -> FlowSpec:
    return flow_from_mapping(
        {
            "flow": {
                "name": "review <flow>",
                "version": 2,
                "description": "Review safely.",
                "mode": "workspace-write",
                "thinking": "xhigh",
            },
            "review": {
                "start": True,
                "prompt": "Inspect the change.",
                "transitions": [
                    {"if": "the change is ready", "go": "done"},
                    {"if": "CI is still running", "wait": "10m", "go": "review"},
                    {"if": "the change is unsafe", "go": "failed"},
                ],
            },
            "done": {"exit": 0},
            "failed": {"exit": 3, "prompt": "Record why."},
        },
        source_path="/tmp/review.flow",
        digest="digest",
    )


def test_dot_represents_start_waits_and_terminal_outcomes() -> None:
    source = render_dot(sample_flow())

    assert "__start -> n0" in source
    assert 'id="start-edge"' in source
    assert "rankdir=TB" in source
    assert 'id="state-n0"' in source
    assert "review" in source
    assert "EXIT 0" in source
    assert "EXIT 3" in source
    assert 'style="dashed"' in source
    assert 'id="edge-e0"' in source
    assert 'id="edge-e2"' in source
    assert "wait 10m" in source
    assert "the change is ready" in source


def test_only_latest_terminal_transition_constrains_vertical_layout() -> None:
    flow = flow_from_mapping(
        {
            "flow": {"name": "terminal anchors", "version": 2},
            "start": {
                "start": True,
                "transitions": [
                    {"if": "already done", "go": "done"},
                    {"if": "work remains", "go": "work"},
                ],
            },
            "work": {"transitions": [{"go": "done"}]},
            "done": {"exit": 0},
        },
        source_path="/tmp/anchors.flow",
        digest="digest",
    )

    source = render_dot(flow)
    early_exit = next(line for line in source.splitlines() if "n0 -> n2" in line)
    terminal_anchor = next(line for line in source.splitlines() if "n1 -> n2" in line)

    assert "constraint=false" in early_exit
    assert "constraint=false" not in terminal_anchor


def test_html_embeds_svg_and_safe_inspector_data_without_network_dependencies() -> None:
    flow = sample_flow()
    states = dict(flow.states)
    states["review"] = replace(states["review"], prompt="</script><script>alert(1)</script>")
    flow = replace(flow, description="</script><script>alert(1)</script>", states=states)

    document = render_html(flow, '<svg viewBox="0 0 100 50"><g id="state-n0"></g></svg>')

    assert '<svg viewBox="0 0 100 50">' in document
    assert "https://" not in document
    assert "http://" not in document
    assert "\\u003c/script\\u003e" in document
    assert "</script><script>alert(1)</script>" not in document
    assert "Fit width" in document
    assert 'id="zoom-in"' in document
    assert 'id="zoom-out"' in document
    assert "const defaultWidth = intrinsicWidth * 0.8" in document
    assert "setScale(Math.min(1, available / defaultWidth))" in document
    assert 'id="viewport"' not in document
    assert "viewport.scrollTo" not in document
    assert "scrollIntoView" in document
    assert "function centerStateHorizontally(state)" in document
    assert "function syncViewportWidth()" in document
    assert "width: var(--viewport-width)" in document
    assert 'class="inspector-grid"' in document
    assert 'class="inspector" aria-live="polite" hidden' in document
    assert "Transitions" in document
    assert "function revealState(state)" in document
    assert "Show ${transition.target}" in document
    assert "target.style.setProperty('--target-color', stateColor(transition.target))" in document
    assert "color: var(--target-color, var(--violet))" in document
    assert "inbound-edge" in document
    assert "outbound-edge" in document
    assert "inbound-node" in document
    assert "outbound-node" in document
    assert "function stateColor(name)" in document
    assert "function applyOverviewColors()" in document
    assert "function applyTooltips()" in document
    assert "title.textContent = text" in document
    assert "const condition = transition.condition || 'always'" in document
    assert "applyTooltips();" in document
    assert "function showOverview()" in document
    assert "stage.classList.add('overview')" in document
    assert ".stage:not(.overview) .node" in document
    assert ".stage:not(.overview) .edge" in document
    assert "stroke: var(--node-color, var(--muted))" in document
    assert "stroke: var(--destination-color, var(--muted))" in document
    assert "drop-shadow" not in document
    assert "if (state.start) document.getElementById('start-edge')?.classList.add('inbound-edge')" in document
    assert "!event.target.closest('.node')" in document

    match = re.search(r'<script id="flow-data" type="application/json">(.*?)</script>', document)
    assert match is not None
    payload = json.loads(match.group(1))
    assert [transition["id"] for transition in payload["states"][0]["transitions"]] == [
        "e0",
        "e1",
        "e2",
    ]


def test_light_theme_changes_both_page_and_graph_palette() -> None:
    flow = sample_flow()

    source = render_dot(flow, theme="light")
    document = render_html(flow, "<svg></svg>", theme="light")

    assert 'fillcolor="#e3f3f6"' in source
    assert 'fillcolor="#e7f4ea"' in source
    assert 'fillcolor="#fae9eb"' in source
    assert '<html lang="en" data-theme="light">' in document
    assert "color-scheme: light" in document
    assert "--canvas: #f3f6f8" in document


def test_render_svg_invokes_graphviz_without_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, '<?xml version="1.0"?><svg></svg>\n', "")

    monkeypatch.setattr("flow.v2.chart.subprocess.run", run)

    assert render_svg(sample_flow(), executable="/opt/bin/dot") == "<svg></svg>\n"
    assert seen["command"] == ["/opt/bin/dot", "-Tsvg"]
    assert seen["check"] is False
    assert seen["timeout"] == 30
    assert "digraph flow" in str(seen["input"])


def test_render_svg_reports_missing_graphviz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("flow.v2.chart.shutil.which", lambda _name: None)

    with pytest.raises(ChartRenderError, match="Graphviz 'dot' was not found"):
        render_svg(sample_flow())


@pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz is not installed")
def test_real_graphviz_output_contains_clickable_state_ids() -> None:
    svg = render_svg(sample_flow())

    assert 'class="node"' in svg
    assert 'id="state&#45;n0"' in svg or 'id="state-n0"' in svg
    assert 'id="start&#45;edge"' in svg or 'id="start-edge"' in svg
    assert 'id="edge&#45;e0"' in svg or 'id="edge-e0"' in svg
    assert "review" in svg
