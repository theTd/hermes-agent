import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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

  if (forceExpanded) {
    return (
      <Card
        className="min-w-0 max-w-full"
        data-inspector-section-collapsible="false"
        data-inspector-section-expanded="true"
        data-inspector-section-forced-open="true"
      >
        <CardHeader className="px-3 py-2">
          <CardTitle className="text-xs font-compressed uppercase tracking-widest">{title}</CardTitle>
        </CardHeader>
        <CardContent className="min-w-0 px-3 py-2">{children}</CardContent>
      </Card>
    );
  }

  if (collapsible) {
    return (
      <Card
        className="min-w-0 max-w-full"
        data-inspector-section-collapsible="true"
        data-inspector-section-expanded={expanded ? "true" : "false"}
      >
        <CardHeader className="px-3 py-2">
          <div className="flex items-center justify-between gap-3">
            <CardTitle className="text-xs font-compressed uppercase tracking-widest">{title}</CardTitle>
            <button
              type="button"
              className="text-[10px] font-mono-ui uppercase tracking-wider opacity-60 transition-opacity hover:opacity-100"
              aria-expanded={expanded}
              onClick={() => setExpanded((value) => !value)}
            >
              {expanded ? "collapse" : "expand"}
            </button>
          </div>
        </CardHeader>
        {expanded ? <CardContent className="min-w-0 px-3 py-2">{children}</CardContent> : null}
      </Card>
    );
  }

  return (
    <Card className="min-w-0 max-w-full">
      <CardHeader className="px-3 py-2">
        <CardTitle className="text-xs font-compressed uppercase tracking-widest">{title}</CardTitle>
      </CardHeader>
      <CardContent className="min-w-0 px-3 py-2">{children}</CardContent>
    </Card>
  );
}
