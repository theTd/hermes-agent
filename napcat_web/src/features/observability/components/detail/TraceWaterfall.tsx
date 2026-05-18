import type { LiveTraceGroup } from "../../types";

function fmtDuration(durationMs: number): string {
  if (durationMs < 1000) return `${durationMs.toFixed(0)}ms`;
  return `${(durationMs / 1000).toFixed(1)}s`;
}

const STAGE_COLORS: Record<string, string> = {
  inbound: "#4ade80",
  trigger: "#ffbd38",
  context: "#60a5fa",
  model: "#a78bfa",
  tools: "#f472b6",
  memory_skill_routing: "#2dd4bf",
  final: "#4ade80",
  error: "#fb2c36",
  raw: "#8aaa9a",
};

export function TraceWaterfall({
  group,
  onSelectLane,
}: {
  group: LiveTraceGroup;
  onSelectLane?: (laneKey: string) => void;
}) {
  const stages = group.stages.filter((s) => s.events.length > 0);
  if (!stages.length) {
    return (
      <div className="rounded bg-background/20 p-3"
      >
        <div className="text-xs font-mono-ui opacity-40"
        >No stage data available.</div>
      </div>
    );
  }

  const startTs = group.startedAt;
  const endTs = group.endedAt ?? group.lastEventAt;
  const totalMs = Math.max((endTs - startTs) * 1000, 1);

  const stageBars = stages.map((stage) => {
    const times = stage.events.map((e) => e.ts).sort((a, b) => a - b);
    const stageStart = times[0];
    const stageEnd = times[times.length - 1];
    const offsetMs = (stageStart - startTs) * 1000;
    const durationMs = Math.max((stageEnd - stageStart) * 1000, 0);
    const leftPct = (offsetMs / totalMs) * 100;
    const rawWidthPct = (durationMs / totalMs) * 100;
    const widthPct = Math.max(rawWidthPct, 0.5);
    return { stage, offsetMs, durationMs, leftPct, widthPct };
  });


  return (
    <div className="space-y-2"
    >
      <div className="flex items-center justify-between"
      >
        <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/60"
        >
          Stage Timeline · total {fmtDuration(totalMs)}
        </div>
        <div className="flex flex-wrap gap-2 text-[10px] font-mono-ui opacity-60"
        >
          {stages.map((s) => (
            <span key={s.key} className="flex items-center gap-1"
            >
              <span className="inline-block h-2 w-2 rounded-sm" style={{ backgroundColor: STAGE_COLORS[s.key] || "#8aaa9a" }}
              />
              {s.key}
            </span>
          ))}
        </div>
      </div>

      <div className="space-y-1"
      >
        {stageBars.map(({ stage, durationMs, leftPct, widthPct }) => {
          const color = STAGE_COLORS[stage.key] || "#8aaa9a";
          const handleStageClick = () => {
            if (!onSelectLane) return;
            if (stage.key === "model" || stage.key === "tools") {
              const agentLane = group.timeline.lanes.find((l) => l.key === "main_agent");
              if (agentLane) {
                onSelectLane(agentLane.key);
                return;
              }
            }
            const firstLane = group.timeline.lanes[0];
            if (firstLane) onSelectLane(firstLane.key);
          };

          return (
            <div key={stage.key} className="flex items-center gap-2">
              <div className="w-28 shrink-0 text-right text-[10px] font-mono-ui opacity-70">
                {stage.title}
              </div>
              <div className="min-w-0 flex-1">
                <div className="relative h-5 rounded bg-background/20">
                  <div
                    className={`absolute top-1 h-3 rounded transition-all duration-150 ${onSelectLane ? "cursor-pointer hover:h-4 hover:top-0.5 hover:opacity-100 hover:ring-2 hover:ring-white/30" : ""}`}
                    style={{
                      left: `${Math.min(leftPct, 99)}%`,
                      width: `${Math.min(widthPct, 100 - leftPct)}%`,
                      backgroundColor: color,
                      opacity: stage.status === "error" ? 1 : 0.8,
                    }}
                    title={`${stage.title}: ${fmtDuration(durationMs)} (${stage.events.length} events)${onSelectLane ? " · click to view details" : ""}`}
                    onClick={handleStageClick}
                  />
                </div>
              </div>
              <div className="w-16 shrink-0 text-right text-[10px] font-mono-ui opacity-60">
                {fmtDuration(durationMs)}
              </div>
            </div>
          );
        })}
      </div>

      {/* Lane-level waterfall */}
      {group.timeline.laneCount > 1 && (
        <div className="mt-3 space-y-1">
          <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/60">
            Lane Timeline
          </div>
          {group.timeline.lanes.map((lane) => {
            const laneStart = lane.startedAt ?? startTs;
            const laneEnd = lane.endedAt ?? endTs;
            const offsetMs = (laneStart - startTs) * 1000;
            const durationMs = Math.max((laneEnd - laneStart) * 1000, 0);
            const leftPct = (offsetMs / totalMs) * 100;
            const widthPct = Math.max((durationMs / totalMs) * 100, 0.5);

            return (
              <div key={lane.key} className="flex items-center gap-2">
                <div className="w-28 shrink-0 text-right text-[10px] font-mono-ui opacity-70">
                  {lane.title}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="relative h-5 rounded bg-background/20">
                    <div
                      className={`absolute top-1 h-3 rounded bg-foreground/40 transition-all duration-150 ${onSelectLane ? "cursor-pointer hover:h-4 hover:top-0.5 hover:bg-foreground/70 hover:ring-2 hover:ring-white/20" : ""}`}
                      style={{
                        left: `${Math.min(leftPct, 99)}%`,
                        width: `${Math.min(widthPct, 100 - leftPct)}%`,
                      }}
                      title={`${lane.title}: ${fmtDuration(durationMs)}${onSelectLane ? " · click to view details" : ""}`}
                      onClick={() => onSelectLane?.(lane.key)}
                    />
                  </div>
                </div>
                <div className="w-16 shrink-0 text-right text-[10px] font-mono-ui opacity-60">
                  {fmtDuration(durationMs)}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
