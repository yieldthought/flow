import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LinkifiedText, linkParts } from "./LinkifiedText";

describe("LinkifiedText", () => {
  it("renders http links as anchors while leaving other schemes as text", () => {
    render(<LinkifiedText text="See https://example.com/a and http://example.com/b. Ignore ftp://example.com/c" />);

    expect(screen.getByRole("link", { name: "https://example.com/a" })).toHaveAttribute("href", "https://example.com/a");
    expect(screen.getByRole("link", { name: "http://example.com/b" })).toHaveAttribute("href", "http://example.com/b");
    expect(screen.queryByRole("link", { name: /ftp:/ })).not.toBeInTheDocument();
  });

  it("strips trailing punctuation from hrefs", () => {
    expect(linkParts("Issue https://github.com/example/repo/issues/1.")).toEqual([
      { text: "Issue " },
      { text: "https://github.com/example/repo/issues/1", href: "https://github.com/example/repo/issues/1" },
      { text: "." },
    ]);
  });
});
