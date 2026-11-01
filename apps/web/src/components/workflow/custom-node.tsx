"use client";

import { Handle, Position, type NodeProps } from "@xyflow/react";
import { getDescriptor, summarizeNode } from "@/lib/workflow/node-catalog";
import type { WorkflowNode } from "@/lib/workflow-types";

export interface WorkflowNodeData extends Record<string, unknown> {
  raw: WorkflowNode;
}

/** 타입별 아이콘·색·요약 1줄을 가진 커스텀 노드 ("${type} · ${id}" 라벨 대체) */
export function WorkflowCanvasNode({ data, selected }: NodeProps) {
  const raw = (data as WorkflowNodeData).raw;
  const desc = getDescriptor(raw.type);
  const Icon = desc?.icon;
  const color = desc?.color ?? "#888";
  const summary = summarizeNode(raw);

  return (
    <div
      className="w-44 rounded-lg border-2 bg-background shadow-sm"
      style={{ borderColor: selected ? color : `${color}80` }}
      data-testid={`wf-node-${raw.id}`}
    >
      <Handle type="target" position={Position.Left} style={{ background: color }} />
      <div className="flex items-center gap-1.5 px-2 py-1.5" style={{ color }}>
        {Icon && <Icon className="size-3.5 shrink-0" />}
        <span className="truncate text-xs font-semibold">{desc?.label ?? raw.type}</span>
      </div>
      <div className="border-t px-2 py-1">
        <p className="truncate font-mono text-xs text-muted-foreground">{raw.id}</p>
        {summary && (
          <p className="line-clamp-2 text-xs text-foreground/70" title={summary}>
            {summary}
          </p>
        )}
      </div>
      {!desc?.terminal && (
        <Handle type="source" position={Position.Right} style={{ background: color }} />
      )}
    </div>
  );
}

export const WORKFLOW_NODE_TYPES = { workflow: WorkflowCanvasNode };
