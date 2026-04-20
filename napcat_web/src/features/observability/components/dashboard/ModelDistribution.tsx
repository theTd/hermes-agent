export function ModelDistribution({
  models,
}: {
  models: Array<{ model: string; count: number }>;
}) {
  if (!models.length) {
    return (
      <div className="rounded bg-background/20 p-3">
        <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/60">Model Distribution</div>
        <div className="mt-2 text-xs font-mono-ui opacity-40">no data</div>
      </div>
    );
  }

  const total = models.reduce((sum, m) => sum + m.count, 0);
  const maxCount = Math.max(...models.map((m) => m.count), 1);

  return (
    <div className="rounded bg-background/20 p-3">
      <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/60">Model Distribution</div>
      <div className="mt-2 space-y-1.5">
        {models.map((m) => (
          <div key={m.model} className="flex items-center gap-2">
            <div className="w-24 shrink-0 truncate text-xs font-mono-ui" title={m.model}>{m.model}</div>
            <div className="min-w-0 flex-1">
              <div
                className="h-2 rounded bg-primary/60"
                style={{ width: `${(m.count / maxCount) * 100}%` }}
              />
            </div>
            <div className="w-10 shrink-0 text-right text-[10px] font-mono-ui opacity-70">
              {((m.count / total) * 100).toFixed(0)}%
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
