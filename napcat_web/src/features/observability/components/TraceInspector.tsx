import { TraceDetailView } from "./TraceDetailView";
import type { ObservabilityController } from "../types";
export {
  buildLlmUsageCostBreakdowns,
  buildTraceDetailViewModel,
  collectContextTextBlocks,
  collectLlmUsageCostItems,
  collectTriggerTextBlocks,
  summarizeLlmUsageCostItems,
} from "../domain/detail-view";

export function TraceInspector({ controller }: { controller: ObservabilityController }) {
  return <TraceDetailView controller={controller} />;
}
