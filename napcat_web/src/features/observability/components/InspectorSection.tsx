import { useEffect, useState } from "react";
import type { ReactNode } from "react";

export function InspectorSection({
  title,
  children,
  collapsible = false,
  defaultExpanded = true,
  forceExpanded = false,
  resetKey,
}: {
  title: string;
  children: ReactNode;
  collapsible?: boolean;
  defaultExpanded?: boolean;
  forceExpanded?: boolean;
  resetKey?: string;
}) {
  const [expanded, setExpanded] = useState(defaultExpanded);

  useEffect(() => {
    setExpanded(defaultExpanded);
  }, [defaultExpanded, resetKey]);

  const header = (
    <div className="flex items-center justify-between gap-3 px-3 py-2">
      <h3 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground/80">{title}</h3>
      {collapsible && !forceExpanded && (
        <button
          type="button"
          className="text-[10px] font-mono-ui uppercase tracking-wider opacity-60 transition-opacity hover:opacity-100"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "collapse" : "expand"}
        </button>
      )}
    </div>
  );

  if (forceExpanded) {
    return (
      <section
        className="min-w-0 max-w-full"
        data-inspector-section-collapsible="false"
        data-inspector-section-expanded="true"
        data-inspector-section-forced-open="true"
      >
        {header}
        <div className="min-w-0 px-3 py-2">{children}</div>
      </section>
    );
  }

  if (collapsible) {
    return (
      <section
        className="min-w-0 max-w-full"
        data-inspector-section-collapsible="true"
        data-inspector-section-expanded={expanded ? "true" : "false"}
      >
        {header}
        <div
          className={`min-w-0 grid transition-[grid-template-rows] duration-300 ease-in-out ${expanded ? "grid-rows-[1fr]" : "grid-rows-[0fr]"}`}
        >
          <div className="min-w-0 overflow-hidden">
            <div className={`px-3 py-2 transition-opacity duration-300 ease-in-out ${expanded ? "opacity-100" : "opacity-0"}`}>
              {children}
            </div>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="min-w-0 max-w-full">
      {header}
      <div className="min-w-0 px-3 py-2">{children}</div>
    </section>
  );
}
