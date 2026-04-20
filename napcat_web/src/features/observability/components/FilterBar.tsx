import { useEffect, useState } from "react";
import { QuickViewChips } from "./QuickViewChips";
import { presetFiltersForView } from "../selectors";
import type { FilterState, ObservabilityController, ObservabilityView } from "../types";

export function FilterBar({ controller }: { controller: ObservabilityController }) {
  const { filters } = controller.state;
  const [searchValue, setSearchValue] = useState(filters.search);

  useEffect(() => {
    setSearchValue(filters.search);
  }, [filters.search]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (searchValue !== filters.search) {
        controller.setFilters({ search: searchValue });
      }
    }, 150);
    return () => window.clearTimeout(timer);
  }, [controller, filters.search, searchValue]);

  function applyView(view: ObservabilityView) {
    controller.setFilters(presetFiltersForView(view));
  }

  function updateField<K extends keyof FilterState>(key: K, value: FilterState[K]) {
    controller.setFilters({ [key]: value } as Partial<FilterState>);
  }

  return (
    <div className="border-b border-border/20 px-3 py-2 bg-background/40">
      <div className="flex flex-col gap-2">
        <QuickViewChips currentView={filters.view} onSelect={applyView} />
        <div className="grid gap-1.5 md:grid-cols-2 xl:grid-cols-5">
          <input id="obs-search-input" value={searchValue} onChange={(event) => setSearchValue(event.target.value)} placeholder="Search traces / payloads" className="rounded bg-card/40 px-2.5 py-1.5 text-xs font-mono-ui md:col-span-2" />
          <input value={filters.eventType} onChange={(event) => updateField("eventType", event.target.value)} placeholder="event_type" className="rounded bg-card/40 px-2.5 py-1.5 text-xs font-mono-ui" />
          <input value={filters.chatId} onChange={(event) => updateField("chatId", event.target.value)} placeholder="chat" className="rounded bg-card/40 px-2.5 py-1.5 text-xs font-mono-ui" />
          <input value={filters.userId} onChange={(event) => updateField("userId", event.target.value)} placeholder="user" className="rounded bg-card/40 px-2.5 py-1.5 text-xs font-mono-ui" />
        </div>
        <div className="flex items-center justify-between">
          <div className="flex flex-wrap gap-1.5 text-xs font-mono-ui">
            {(["", "in_progress", "completed", "ignored", "error", "rejected"] as const).map((status) => (
              <button
                key={status || "any"}
                type="button"
                onClick={() => updateField("status", status)}
                className={`rounded px-2 py-0.5 transition-colors ${
                  filters.status === status
                    ? "bg-primary/15 text-foreground"
                    : "opacity-60 hover:opacity-100"
                }`}
              >
                {status || "any"}
              </button>
            ))}
          </div>
          <button type="button" onClick={controller.clearFilters} className="text-xs font-mono-ui opacity-60 hover:opacity-100">
            clear
          </button>
        </div>
      </div>
    </div>
  );
}
