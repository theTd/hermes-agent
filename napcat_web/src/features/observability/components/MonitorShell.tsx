import type { ReactNode } from "react";

export function MonitorShell({
  topBar,
  filterBar,
  leftRail,
  requestPane,
  detailPane,
}: {
  topBar: ReactNode;
  filterBar: ReactNode;
  leftRail: ReactNode;
  requestPane: ReactNode;
  detailPane: ReactNode;
}) {
  return (
    <div className="flex min-h-screen flex-col">
      <div className="sticky top-0 z-20 backdrop-blur-md">
        {topBar}
        {filterBar}
      </div>
      <div className="grid min-h-0 flex-1 gap-px border-t border-border/20 bg-border/15 xl:grid-cols-[18rem_minmax(21rem,25rem)_minmax(0,1fr)]">
        <section className="min-h-0 min-w-0 overflow-hidden bg-card/20">
          {leftRail}
        </section>
        <section className="min-h-0 min-w-0 overflow-hidden bg-background/25">
          {requestPane}
        </section>
        <main className="min-h-0 min-w-0 overflow-hidden bg-card/10">
          {detailPane}
        </main>
      </div>
    </div>
  );
}
