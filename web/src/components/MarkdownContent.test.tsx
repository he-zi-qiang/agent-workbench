import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MarkdownContent } from "./MarkdownContent";

describe("MarkdownContent", () => {
  it("does not publish script tags or dangerous links", () => {
    const { container } = render(
      <MarkdownContent text={'safe<script>alert("x")</script> [bad](javascript:alert(1))'} />,
    );

    expect(screen.getByText(/safe/)).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    const href = container.querySelector("a")?.getAttribute("href");
    expect(href === undefined || href === null || !href.startsWith("javascript:")).toBe(
      true,
    );
  });
});
