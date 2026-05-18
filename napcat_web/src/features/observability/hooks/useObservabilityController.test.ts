import { describe, expect, it } from "vitest";

import { shouldHydrateTraceDetails } from "./useObservabilityController";

describe("shouldHydrateTraceDetails", () => {
  it("requests full trace when summary exists but events are missing", () => {
    expect(shouldHydrateTraceDetails({
      event_count: 6,
      latest_stage: "final",
      summary: { headline: "old trace" },
    }, [])).toBe(true);
  });

  it("skips hydration after events are already present", () => {
    expect(shouldHydrateTraceDetails({
      event_count: 6,
      latest_stage: "final",
      summary: { headline: "old trace" },
    }, [{ event_id: "evt-1" }])).toBe(false);
  });

  it("skips hydration for empty placeholder traces", () => {
    expect(shouldHydrateTraceDetails({
      event_count: 0,
      latest_stage: "",
      summary: {},
    }, [])).toBe(false);
  });
});
