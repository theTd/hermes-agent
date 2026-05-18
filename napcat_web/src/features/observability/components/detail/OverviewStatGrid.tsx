export function OverviewStatGrid({
  entries,
}: {
  entries: Array<{ label: string; value: string }>;
}) {
  return (
    <div
      className="grid grid-cols-1 gap-1.5 sm:grid-cols-2 xl:grid-cols-4"
      data-overview-grid="true"
    >
      {entries.map((entry) => (
        <div
          key={entry.label}
          className="min-w-0 rounded bg-background/20 px-2 py-1.5"
          data-overview-stat={entry.label}
        >
          <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/70">
            {entry.label}
          </div>
          <div
            className="mt-0.5 min-w-0 break-all text-[11px] font-mono-ui leading-4 text-foreground [overflow-wrap:anywhere]"
            title={entry.value || "—"}
          >
            {entry.value || "—"}
          </div>
        </div>
      ))}
    </div>
  );
}
