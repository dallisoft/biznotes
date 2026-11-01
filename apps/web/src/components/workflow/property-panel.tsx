"use client";

import { useRef, useState } from "react";
import { Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useCallableAgents } from "@/hooks/use-workflows";
import { getDescriptor, type NodeField } from "@/lib/workflow/node-catalog";
import type { WorkflowNode } from "@/lib/workflow-types";

/** prompt 컨트롤 — 변수 토큰 칩을 커서 위치에 삽입 */
function PromptField({
  field,
  value,
  variables,
  onChange,
}: {
  field: NodeField;
  value: string;
  variables: string[];
  onChange: (v: string) => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);

  function insert(token: string) {
    const el = ref.current;
    if (!el) {
      onChange(value + token);
      return;
    }
    const start = el.selectionStart ?? value.length;
    const end = el.selectionEnd ?? value.length;
    const next = value.slice(0, start) + token + value.slice(end);
    onChange(next);
    requestAnimationFrame(() => {
      el.focus();
      const pos = start + token.length;
      el.setSelectionRange(pos, pos);
    });
  }

  return (
    <div className="flex flex-col gap-1.5">
      <Textarea
        ref={ref}
        rows={4}
        value={value}
        placeholder={field.placeholder}
        spellCheck={false}
        className="font-mono text-xs"
        onChange={(e) => onChange(e.target.value)}
      />
      {variables.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {variables.map((v) => (
            <button
              key={v}
              type="button"
              onClick={() => insert(v)}
              className="rounded bg-primary/10 px-1.5 py-0.5 font-mono text-xs text-primary hover:bg-primary/20"
              title="클릭하여 삽입"
            >
              {v}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** JSON 컨트롤 — 로컬 텍스트 편집 + 파싱 성공 시에만 반영 */
function JsonField({
  value,
  onChange,
}: {
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const [text, setText] = useState(() => JSON.stringify(value ?? {}, null, 2));
  const [error, setError] = useState<string | null>(null);

  return (
    <div className="flex flex-col gap-1">
      <Textarea
        rows={5}
        value={text}
        spellCheck={false}
        className="font-mono text-xs"
        onChange={(e) => {
          setText(e.target.value);
          try {
            onChange(JSON.parse(e.target.value));
            setError(null);
          } catch (err) {
            setError(err instanceof Error ? err.message : "JSON 파싱 실패");
          }
        }}
      />
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

/** MCP 도구 선택 — server_id + tool_name 텍스트(사용자 MCP 목록 API 부재 시 수동 입력) */
function McpToolPicker({
  node,
  onPatch,
}: {
  node: WorkflowNode;
  onPatch: (patch: Record<string, unknown>) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      <Input
        value={String(node.server_id ?? "")}
        placeholder="MCP 서버 ID (UUID)"
        onChange={(e) => onPatch({ server_id: e.target.value })}
      />
      <Input
        value={String(node.tool_name ?? "")}
        placeholder="도구 이름"
        onChange={(e) => onPatch({ tool_name: e.target.value })}
      />
      <p className="text-xs text-muted-foreground">
        MCP 서버 ID·도구 이름은 관리 → MCP 서버에서 확인하세요.
      </p>
    </div>
  );
}

/** 서브에이전트 선택 — 호출 가능 워크플로우/스킬 드롭다운 + 파라미터 힌트 */
function AgentPicker({
  node,
  onPatch,
}: {
  node: WorkflowNode;
  onPatch: (patch: Record<string, unknown>) => void;
}) {
  const callable = useCallableAgents();
  const list = callable.data ?? [];
  const selected = list.find((a) => a.id === node.agent_id);
  const paramKeys = Object.keys(selected?.params_schema?.properties ?? {});

  return (
    <div className="flex flex-col gap-2">
      <Select
        value={String(node.agent_id ?? "")}
        onValueChange={(v) => v && onPatch({ agent_id: v })}
      >
        <SelectTrigger className="w-full" data-testid="agent-picker">
          <SelectValue placeholder="에이전트 선택" />
        </SelectTrigger>
        <SelectContent>
          {list.map((a) => (
            <SelectItem key={a.id} value={a.id}>
              {a.icon} {a.name} ({a.kind})
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {list.length === 0 && !callable.isPending && (
        <p className="text-xs text-muted-foreground">호출 가능한 에이전트가 없습니다.</p>
      )}
      {paramKeys.length > 0 && (
        <p className="text-xs text-muted-foreground">
          대상 파라미터: {paramKeys.map((k) => `{params.${k}}`).join(", ")}
        </p>
      )}
    </div>
  );
}

/** 읽기 전용 리터럴 — 고칠 수 없는 값을 그대로 보여 준다(없으면 「(없음)」) */
function ReadOnlyValue({ field, value }: { field: NodeField; value: unknown }) {
  const text =
    value === undefined || value === null || value === ""
      ? "(없음)"
      : typeof value === "string"
        ? value
        : JSON.stringify(value, null, 2);
  return (
    <pre
      data-testid={`readonly-${field.key}`}
      aria-readonly="true"
      className="whitespace-pre-wrap break-all rounded border bg-muted/40 px-2 py-1.5 font-mono text-xs text-muted-foreground"
    >
      {text}
    </pre>
  );
}

function FieldControl({
  field,
  node,
  variables,
  onPatch,
}: {
  field: NodeField;
  node: WorkflowNode;
  variables: string[];
  onPatch: (patch: Record<string, unknown>) => void;
}) {
  const value = node[field.key];

  if (field.readOnly) {
    return <ReadOnlyValue field={field} value={value} />;
  }

  switch (field.control) {
    case "prompt":
      return (
        <PromptField
          field={field}
          value={String(value ?? "")}
          variables={variables}
          onChange={(v) => onPatch({ [field.key]: v })}
        />
      );
    case "textarea":
      return (
        <Textarea
          rows={3}
          value={String(value ?? "")}
          placeholder={field.placeholder}
          className="text-xs"
          onChange={(e) => onPatch({ [field.key]: e.target.value })}
        />
      );
    case "number":
      return (
        <Input
          type="number"
          value={value === undefined || value === null ? "" : String(value)}
          placeholder={field.placeholder}
          onChange={(e) =>
            onPatch({ [field.key]: e.target.value === "" ? undefined : Number(e.target.value) })
          }
        />
      );
    case "select":
      return (
        <Select
          value={String(value ?? "")}
          onValueChange={(v) => v && onPatch({ [field.key]: v })}
        >
          <SelectTrigger className="w-full">
            <SelectValue placeholder="선택" />
          </SelectTrigger>
          <SelectContent>
            {(field.options ?? []).map((o) => (
              <SelectItem key={o.value} value={o.value}>
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      );
    case "multiselect": {
      const selected = Array.isArray(value) ? (value as string[]) : [];
      const toggle = (v: string) => {
        const next = selected.includes(v)
          ? selected.filter((s) => s !== v)
          : [...selected, v];
        onPatch({ [field.key]: next });
      };
      return (
        <div className="flex flex-col gap-1.5">
          {(field.options ?? []).map((o) => (
            <label key={o.value} className="flex items-center gap-2 text-xs">
              <input
                type="checkbox"
                checked={selected.includes(o.value)}
                onChange={() => toggle(o.value)}
              />
              {o.label}
            </label>
          ))}
        </div>
      );
    }
    case "json":
      return <JsonField value={value} onChange={(v) => onPatch({ [field.key]: v })} />;
    case "mcp-tool-picker":
      return <McpToolPicker node={node} onPatch={onPatch} />;
    case "agent-picker":
      return <AgentPicker node={node} onPatch={onPatch} />;
    default:
      return (
        <Input
          value={String(value ?? "")}
          placeholder={field.placeholder}
          onChange={(e) => onPatch({ [field.key]: e.target.value })}
        />
      );
  }
}

export function PropertyPanel({
  node,
  variables,
  onPatch,
  onDelete,
}: {
  node: WorkflowNode;
  variables: string[];
  onPatch: (patch: Record<string, unknown>) => void;
  onDelete: () => void;
}) {
  const desc = getDescriptor(node.type);
  if (!desc) {
    return (
      <p className="p-4 text-xs text-destructive">알 수 없는 노드 타입: {node.type}</p>
    );
  }
  const Icon = desc.icon;

  return (
    <div className="flex h-full flex-col" data-testid="property-panel">
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <Icon className="size-4" style={{ color: desc.color }} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold">{desc.label}</p>
          <p className="truncate font-mono text-xs text-muted-foreground">{node.id}</p>
        </div>
      </div>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        <p className="text-xs text-muted-foreground">{desc.description}</p>
        {desc.fields.map((field) => (
          <div key={field.key} className="flex flex-col gap-1">
            <Label className="text-xs">
              {field.label}
              {field.required && <span className="ml-0.5 text-destructive">*</span>}
            </Label>
            <FieldControl
              field={field}
              node={node}
              variables={variables}
              onPatch={onPatch}
            />
            {field.help && <p className="text-xs text-muted-foreground">{field.help}</p>}
          </div>
        ))}
      </div>
      <div className="border-t p-3">
        <Button
          variant="ghost"
          size="sm"
          className="w-full text-destructive hover:text-destructive"
          onClick={onDelete}
        >
          <Trash2 className="size-4" /> 노드 삭제
        </Button>
      </div>
    </div>
  );
}
