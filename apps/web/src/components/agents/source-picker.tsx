"use client";

import { Checkbox } from "@/components/ui/checkbox";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useSources } from "@/hooks/use-sources";
import { sourceTypeIcon } from "@/lib/source-types";

/**
 * scope:selected 실행용 노트북 소스 멀티셀렉트.
 * 선택값(source_ids)은 상위에서 createRun 페이로드로 전송. 선택 없음 = 노트 전체(scope:all 폴백).
 */
export function SourcePicker({
  notebookId,
  selected,
  onChange,
}: {
  notebookId: string;
  selected: string[];
  onChange: (ids: string[]) => void;
}) {
  const sources = useSources(notebookId);
  const ready = (sources.data ?? []).filter((s) => s.status === "ready");

  function toggle(id: string) {
    onChange(selected.includes(id) ? selected.filter((s) => s !== id) : [...selected, id]);
  }

  if (sources.isPending) {
    return <p className="text-xs text-muted-foreground">소스 불러오는 중...</p>;
  }
  if (ready.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        준비된 소스가 없습니다. 선택 없이 실행하면 노트 전체를 대상으로 합니다.
      </p>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium">소스 선택</span>
        <button
          type="button"
          className="text-xs text-muted-foreground hover:text-foreground"
          onClick={() => onChange(selected.length === ready.length ? [] : ready.map((s) => s.id))}
        >
          {selected.length === ready.length ? "전체 해제" : "전체 선택"}
        </button>
      </div>
      <ScrollArea className="max-h-48 min-w-0 rounded-md border">
        <ul className="flex flex-col p-1" data-testid="source-picker-list">
          {ready.map((s) => (
            <li key={s.id}>
              <label className="flex min-w-0 cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-xs hover:bg-accent/60">
                <Checkbox
                  checked={selected.includes(s.id)}
                  onCheckedChange={() => toggle(s.id)}
                  data-testid={`source-option-${s.id}`}
                />
                <span>{sourceTypeIcon(s.type)}</span>
                <span className="min-w-0 flex-1 truncate" title={s.title}>
                  {s.title}
                </span>
              </label>
            </li>
          ))}
        </ul>
      </ScrollArea>
      <span className="text-xs text-muted-foreground">
        {selected.length > 0
          ? `${selected.length}개 소스 선택됨`
          : "선택 없음 → 노트 전체 대상"}
      </span>
    </div>
  );
}
