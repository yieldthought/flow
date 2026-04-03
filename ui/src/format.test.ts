import { edgeSummaryText, edgeTransitionText, formatArgs, formatCountdown, stateSummaryText } from "./format";

describe("format helpers", () => {
  it("formats countdowns with padded clock segments", () => {
    expect(formatCountdown(3723)).toBe("01:02:03");
  });

  it("formats args using sorted key value pairs", () => {
    expect(formatArgs({ site: "news.ycombinator.com", mode: "hn" })).toBe("mode=hn site=news.ycombinator.com");
  });

  it("formats focus summaries compactly", () => {
    expect(
      stateSummaryText({
        state_name: "check",
        count: 4,
        latest_duration_seconds: 120,
        latest_duration_text: "0h 2m",
        total_duration_seconds: 1080,
        total_duration_text: "0h 18m",
        visits: [],
      }),
    ).toBe("x4 latest 0h 2m total 0h 18m");

    expect(
      edgeSummaryText({
        key: "check->check",
        count: 7,
        latest_created_at: "2026-04-02T12:00:00Z",
        latest_time_text: "21:04 on Apr 1",
        latest_reason: "Still running",
        items: [],
      }),
    ).toContain("x7 21:04 on Apr 1");
  });

  it("formats edge transition labels with bracketed waits", () => {
    expect(edgeTransitionText(["[10m wait]"])).toBe("[10m wait]");
    expect(edgeTransitionText([""])).toBe("");
    expect(edgeTransitionText(["[10m wait]"])).toBe("[10m wait]");
  });
});
