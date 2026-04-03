import { buildGraphModel } from "./graph";
import type { OverviewSnapshot } from "./types";

function makeSnapshot(): OverviewSnapshot {
  return {
    runtime: {
      active: true,
      pid: 42,
      started_at: "2026-04-02T10:00:00Z",
      heartbeat_at: "2026-04-02T10:00:01Z",
      uptime_seconds: 60,
      diagnostics: [],
    },
    flow: {
      name: "demo",
      counts: { waiting: 1, working: 1, paused: 0, needs_help: 0 },
      states: [
        {
          id: "check",
          name: "check",
          start: true,
          end: false,
          synthetic: false,
          rows: {
            waiting: [
              {
                id: 7,
                status: "waiting",
                timer_seconds: 60,
                args: { site: "news.ycombinator.com" },
                display_args: { site: "news.ycombinator.com" },
                state_name: "check",
                substate: "normal",
                phase: "waiting",
                cwd: "/tmp/work",
                ready_at: "",
                ended_at: "",
              },
            ],
            working: [],
            paused: [],
            needs_help: [],
            finished: [],
          },
          focus: {
            state_name: "check",
            count: 2,
            latest_duration_seconds: 120,
            latest_duration_text: "0h 2m",
            total_duration_seconds: 480,
            total_duration_text: "0h 8m",
            visits: [],
          },
        },
        {
          id: "done",
          name: "done",
          start: false,
          end: true,
          synthetic: false,
          rows: { waiting: [], working: [], paused: [], needs_help: [], finished: [] },
          focus: {
            state_name: "done",
            count: 0,
            latest_duration_seconds: 0,
            latest_duration_text: "0h 0m",
            total_duration_seconds: 0,
            total_duration_text: "0h 0m",
            visits: [],
          },
        },
      ],
      edges: [
        {
          id: "check->done",
          key: "check->done",
          source: "check",
          target: "done",
          transition_labels: [],
          transition_label_text: "",
          focus: {
            key: "check->done",
            count: 1,
            latest_created_at: "2026-04-02T10:05:00Z",
            latest_time_text: "12:05 on Apr 2",
            latest_reason: "Finished",
            items: [],
          },
        },
      ],
    },
    focus: {
      agent: {
        id: 7,
        flow_name: "demo",
        current_state: "check",
        substate: "normal",
        phase: "working",
        status: "working",
        timer_seconds: 120,
        status_message: "Checking",
        cwd: "/tmp/work",
        args: { site: "news.ycombinator.com" },
        display_args: { site: "news.ycombinator.com" },
        ready_at: "",
        ended_at: "",
        created_at: "2026-04-02T10:00:00Z",
        state_options: ["check", "done"],
      },
      events: [],
      states: {},
      edges: {},
    },
  };
}

describe("graph model", () => {
  it("preserves overview rows in overview mode", () => {
    const snapshot = makeSnapshot();
    delete snapshot.focus;
    const graph = buildGraphModel(snapshot);

    expect(graph.mode).toBe("overview");
    expect(graph.nodes[0]?.data.state.rows.waiting[0]?.id).toBe(7);
  });

  it("hides other agent rows in focus mode while preserving the skeleton", () => {
    const graph = buildGraphModel(makeSnapshot());

    expect(graph.mode).toBe("focus");
    expect(graph.nodes.map((node) => node.id)).toEqual(["check", "done"]);
    expect(graph.nodes[0]?.data.state.rows.waiting).toEqual([]);
    expect(graph.edges[0]?.data?.edge.focus?.count).toBe(1);
  });

  it("routes around a blocking middle state", () => {
    const snapshot = makeSnapshot();
    delete snapshot.focus;
    snapshot.flow.states = [
      snapshot.flow.states[0],
      {
        id: "investigate",
        name: "investigate",
        start: false,
        end: false,
        synthetic: false,
        rows: { waiting: [], working: [], paused: [], needs_help: [], finished: [] },
      },
      snapshot.flow.states[1],
    ];
    snapshot.flow.edges = [
      { id: "check->done", key: "check->done", source: "check", target: "done", transition_labels: [], transition_label_text: "" },
      { id: "check->investigate", key: "check->investigate", source: "check", target: "investigate", transition_labels: ["failed"], transition_label_text: "failed" },
      { id: "investigate->done", key: "investigate->done", source: "investigate", target: "done", transition_labels: [], transition_label_text: "" },
    ];

    const graph = buildGraphModel(snapshot);
    const directEdge = graph.edges.find((edge) => edge.id === "check->done");

    expect(typeof directEdge?.data?.detourY).toBe("number");
  });

  it("adds loop geometry for self-transitions", () => {
    const snapshot = makeSnapshot();
    delete snapshot.focus;
    snapshot.flow.edges = [
      { id: "check->check", key: "check->check", source: "check", target: "check", transition_labels: ["[10m wait]"], transition_label_text: "[10m wait]" },
    ];

    const graph = buildGraphModel(snapshot);

    expect(graph.edges[0]?.data?.selfLoop).toBeTruthy();
  });

  it("aligns simple one-in one-out chains into a straight horizontal run", () => {
    const snapshot = makeSnapshot();
    delete snapshot.focus;
    snapshot.flow.states = [
      { ...snapshot.flow.states[0], id: "check-run", name: "check-run" },
      {
        id: "investigate-failure",
        name: "investigate-failure",
        start: false,
        end: false,
        synthetic: false,
        rows: { waiting: [], working: [], paused: [], needs_help: [], finished: [] },
      },
      {
        id: "notify-failure",
        name: "notify-failure",
        start: false,
        end: false,
        synthetic: false,
        rows: { waiting: [], working: [], paused: [], needs_help: [], finished: [] },
      },
      {
        id: "notify-pass",
        name: "notify-pass",
        start: false,
        end: false,
        synthetic: false,
        rows: { waiting: [], working: [], paused: [], needs_help: [], finished: [] },
      },
    ];
    snapshot.flow.edges = [
      { id: "check-run->investigate-failure", key: "check-run->investigate-failure", source: "check-run", target: "investigate-failure", transition_labels: [], transition_label_text: "" },
      { id: "investigate-failure->notify-failure", key: "investigate-failure->notify-failure", source: "investigate-failure", target: "notify-failure", transition_labels: [], transition_label_text: "" },
      { id: "check-run->notify-pass", key: "check-run->notify-pass", source: "check-run", target: "notify-pass", transition_labels: [], transition_label_text: "" },
    ];

    const graph = buildGraphModel(snapshot);
    const investigate = graph.nodes.find((node) => node.id === "investigate-failure");
    const notifyFailure = graph.nodes.find((node) => node.id === "notify-failure");

    expect(notifyFailure?.position.y).toBe(investigate?.position.y);
  });
});
