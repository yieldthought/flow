import { describe, expect, it } from "vitest";

import { stateNodeClassName } from "./StateNode";

describe("stateNodeClassName", () => {
  it("does not ghost overview nodes", () => {
    expect(stateNodeClassName("overview", false, false)).toContain("state-node--overview");
    expect(stateNodeClassName("overview", false, false)).not.toContain("state-node--ghost");
  });

  it("ghosts only unvisited focus nodes", () => {
    expect(stateNodeClassName("focus", false, false)).toContain("state-node--ghost");
    expect(stateNodeClassName("focus", true, false)).toContain("state-node--visited");
  });
});
