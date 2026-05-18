export function KpiCard({
  title,
  value,
  subtext,
  variant = "default",
}: {
  title: string;
  value: string;
  subtext?: string;
  variant?: "default" | "error" | "success";
}) {
  const valueClass =
    variant === "error"
      ? "text-destructive"
      : variant === "success"
        ? "text-success"
        : "text-foreground";

  return (
    <div className="rounded bg-background/20 p-3">
      <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/60">
        {title}
      </div>
      <div className={`mt-1 text-2xl font-semibold ${valueClass}`}>{value}</div>
      {subtext && (
        <div className="mt-1 text-xs font-mono-ui opacity-60">{subtext}</div>
      )}
    </div>
  );
}
