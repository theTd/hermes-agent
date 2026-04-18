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
    }, 250);
    return () => window.clearTimeout(timer);
  }, [controller, filters.search, searchValue]);

  function applyView(view: ObservabilityView) {
    controller.setFilters(presetFiltersForView(view));
  }

  function updateField<K extends keyof FilterState>(key: K, value: FilterState[K]) {
    controller.setFilters({ [key]: value } as Partial<FilterState>);
  }

  return (
    <div className="border-b border-border/20 px-4 py-3 bg-background/40">
      <div className="flex flex-col gap-3">
        <QuickViewChips currentView={filters.view} onSelect={applyView} />
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-5">
          <input value={searchValue} onChange={(event) => setSearchValue(event.target.value)} placeholder="Search traces / payloads" className="rounded border border-border/40 bg-card/40 px-3 py-2 text-sm font-mono-ui md:col-span-2" />
          <input value={filters.eventType} onChange={(event) => updateField("eventType", event.target.value)} placeholder="event_type" className="rounded border border-border/40 bg-card/40 px-3 py-2 text-sm font-mono-ui" />
          <input value={filters.chatId} onChange={(event) => updateField("chatId", event.target.value)} placeholder="chat" className="rounded border border-border/40 bg-card/40 px-3 py-2 text-sm font-mono-ui" />
          <input value={filters.userId} onChange={(event) => updateField("userId", event.target.value)} placeholder="user" className="rounded border border-border/40 bg-card/40 px-3 py-2 text-sm font-mono-ui" />
        </div>
        <div className="flex items-center justify-between">
          <div className="flex flex-wrap gap-2 text-xs font-mono-ui opacity-70">
            <button type="button" onClick={() => updateField("status", "")} className={filters.status === "" ? "underline" : ""}>any status</button>
            <button type="button" onClick={() => updateField("status", "in_progress")} className={filters.status === "in_progress" ? "underline" : ""}>in_progress</button>
            <button type="button" onClick={() => updateField("status", "completed")} className={filters.status === "completed" ? "underline" : ""}>completed</button>
            <button type="button" onClick={() => updateField("status", "ignored")} className={filters.status === "ignored" ? "underline" : ""}>ignored</button>
            <button type="button" onClick={() => updateField("status", "error")} className={filters.status === "error" ? "underline" : ""}>error</button>
            <button type="button" onClick={() => updateField("status", "rejected")} className={filters.status === "rejected" ? "underline" : ""}>rejected</button>
          </div>
          <button type="button" onClick={controller.clearFilters} className="text-xs font-mono-ui opacity-70 hover:opacity-100">
            Clear filters
          </button>
        </div>
      </div>
    </div>
  );
}
