import { useEffect, useMemo, useState } from "react";
import type { TraceDetailViewModel, TraceTimelineItemViewModel } from "../../domain/detail-view";
import { StreamItemCard } from "./StreamItemCard";

interface FlattenedItem {
  item: TraceTimelineItemViewModel;
  ts: number;
  shareOfTotal: number;
}

interface TreeNode {
  item: TraceTimelineItemViewModel;
  ts: number;
  shareOfTotal: number;
  children: TreeNode[];
}

function getItemTimestamp(item: TraceTimelineItemViewModel): number {
  if (item.kind === "request" && item.block) {
    return item.block.startedAt;
  }
  if (item.kind === "event" && item.entry) {
    return item.entry.ts;
  }
  return 0;
}

function getItemDurationMs(item: TraceTimelineItemViewModel, nowMs: number): number {
  if (item.kind === "request" && item.block) {
    if (item.block.durationMs != null) {
      return item.block.durationMs;
    }
    if (item.block.endedAt != null) {
      return Math.max(0, (item.block.endedAt - item.block.startedAt) * 1000);
    }
    return Math.max(0, (nowMs / 1000 - item.block.startedAt) * 1000);
  }
  if (item.kind === "event" && item.entry) {
    if (item.entry.durationMs != null) {
      return item.entry.durationMs;
    }
    if (item.entry.endedAt != null) {
      return Math.max(0, (item.entry.endedAt - item.entry.startedAt) * 1000);
    }
    return 0;
  }
  return 0;
}

function flattenAndSortItems(viewModel: TraceDetailViewModel): FlattenedItem[] {
  const allItems: { item: TraceTimelineItemViewModel; ts: number }[] = [];

  for (const lane of viewModel.lanes) {
    for (const item of lane.timelineItems) {
      const ts = getItemTimestamp(item);
      allItems.push({ item, ts });
    }
  }

  const totalStartMs = viewModel.group.startedAt * 1000;
  const totalEndMs = viewModel.group.endedAt != null
    ? viewModel.group.endedAt * 1000
    : Date.now();
  const totalDuration = totalEndMs - totalStartMs;
  const nowMs = Date.now();

  return allItems
    .sort((a, b) => a.ts - b.ts)
    .map(({ item, ts }) => {
      const durationMs = getItemDurationMs(item, nowMs);
      const shareOfTotal = totalDuration > 0 ? Math.min(durationMs / totalDuration, 1) : 0;
      return { item, ts, shareOfTotal };
    });
}

function getItemSpanInfo(item: TraceTimelineItemViewModel): { spanId: string | null; parentSpanId: string | null } {
  if (item.kind === "event" && item.entry?.event) {
    return {
      spanId: item.entry.event.span_id || null,
      parentSpanId: item.entry.event.parent_span_id || null,
    };
  }
  if (item.kind === "request" && item.block?.rawEvents?.length) {
    const firstEvent = item.block.rawEvents[0];
    return {
      spanId: firstEvent?.span_id || null,
      parentSpanId: firstEvent?.parent_span_id || null,
    };
  }
  return { spanId: null, parentSpanId: null };
}

function buildTree(flatItems: FlattenedItem[]): TreeNode[] {
  const idToNode = new Map<string, TreeNode>();
  const spanToNodeId = new Map<string, string>();

  for (const flat of flatItems) {
    const { spanId } = getItemSpanInfo(flat.item);
    const node: TreeNode = {
      item: flat.item,
      ts: flat.ts,
      shareOfTotal: flat.shareOfTotal,
      children: [],
    };
    idToNode.set(flat.item.id, node);
    if (spanId) {
      spanToNodeId.set(spanId, flat.item.id);
    }
  }

  const roots: TreeNode[] = [];

  for (const flat of flatItems) {
    const node = idToNode.get(flat.item.id);
    if (!node) continue;

    const { parentSpanId } = getItemSpanInfo(flat.item);
    if (parentSpanId && spanToNodeId.has(parentSpanId)) {
      const parentId = spanToNodeId.get(parentSpanId)!;
      const parent = idToNode.get(parentId);
      if (parent && parent !== node) {
        parent.children.push(node);
      } else {
        roots.push(node);
      }
    } else {
      roots.push(node);
    }
  }

  return roots;
}

interface VisibleNode {
  node: TreeNode;
  depth: number;
  isLast: boolean;
}

function flattenTree(
  nodes: TreeNode[],
  expanded: Set<string>,
  depth: number = 0,
  result: VisibleNode[] = [],
): VisibleNode[] {
  for (let i = 0; i < nodes.length; i++) {
    const node = nodes[i];
    const isLast = i === nodes.length - 1;
    result.push({ node, depth, isLast });

    if (expanded.has(node.item.id) && node.children.length > 0) {
      flattenTree(node.children, expanded, depth + 1, result);
    }
  }
  return result;
}

export function StreamTimeline({
  viewModel,
  onOpenFullscreen,
  traceId,
}: {
  viewModel: TraceDetailViewModel;
  onOpenFullscreen: (block: { label: string; content: string; title?: string }) => void;
  traceId: string;
}) {
  const tree = useMemo(() => {
    const items = flattenAndSortItems(viewModel);
    return buildTree(items);
  }, [viewModel]);

  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set());

  // Auto-expand new nodes when tree changes
  useEffect(() => {
    setExpandedNodes(prev => {
      const next = new Set(prev);
      function addNew(nodes: TreeNode[]) {
        for (const node of nodes) {
          if (!next.has(node.item.id)) {
            next.add(node.item.id);
          }
          addNew(node.children);
        }
      }
      addNew(tree);
      return next;
    });
  }, [tree]);

  const visibleNodes = useMemo(
    () => flattenTree(tree, expandedNodes),
    [tree, expandedNodes],
  );

  const toggleExpand = (id: string) => {
    setExpandedNodes(prev => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  if (visibleNodes.length === 0) {
    return (
      <div className="text-xs font-mono-ui opacity-40 p-4">
        No timeline entries.
      </div>
    );
  }

  return (
    <div className="space-y-0 py-3">
      {visibleNodes.map(({ node, depth, isLast }, index) => (
        <div key={`${traceId}:${node.item.id}`} id={`stream-item-${node.item.id}`}>
          <StreamItemCard
            item={node.item}
            onOpenFullscreen={onOpenFullscreen}
            isLast={isLast && index === visibleNodes.length - 1}
            shareOfTotal={node.shareOfTotal}
            depth={depth}
            hasChildren={node.children.length > 0}
            isExpanded={expandedNodes.has(node.item.id)}
            onToggleExpand={() => toggleExpand(node.item.id)}
          />
        </div>
      ))}
    </div>
  );
}
