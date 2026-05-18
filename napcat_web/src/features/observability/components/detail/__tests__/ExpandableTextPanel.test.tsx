import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { ExpandableTextPanel } from "../ExpandableTextPanel";

describe("ExpandableTextPanel", () => {
  it("renders preview content in preview mode", () => {
    const html = renderToStaticMarkup(
      <ExpandableTextPanel
        title="Response"
        label="req-1.response"
        content="Visible response body"
        visibility="preview"
        onOpenFullscreen={() => {}}
      />,
    );

    expect(html).toContain("Visible response body");
    expect(html).toContain("expand");
    expect(html).toContain("fullscreen");
  });

  it("hides content in hidden mode until expanded", () => {
    const html = renderToStaticMarkup(
      <ExpandableTextPanel
        title="Debug Payload"
        label="agent.memory.used"
        content='{"secret":true}'
        visibility="hidden"
        onOpenFullscreen={() => {}}
      />,
    );

    expect(html).not.toContain("{&quot;secret&quot;:true}");
    expect(html).toContain("expand");
    expect(html).toContain("fullscreen");
  });
});
