"use client";

import { useState } from "react";
import { CapabilityPicker } from "@/components/admin/llm/capability-picker";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useImportModels, useProviderAvailableModels } from "@/hooks/use-admin-llm";
import type { AdminProvider, DiscoveredModel } from "@/lib/admin-types";
import { capabilityLabel } from "@/lib/labels";
import { countModalities, filterByModalities } from "@/lib/modality-filter";

/**
 * 프로바이더가 제공하는 모델을 골라 일괄 등록한다 — 제어형(트리거 없음). 프로바이더 행의 더보기 메뉴
 * 「모델 가져오기」가 연다(§6.6.2 LLM 행). 메뉴 항목은 선택 뒤 사라지므로 닫힐 때의 포커스 복귀 대상은
 * 그 행의 `⋯` 트리거(`returnFocusTo`)다(§6.5 RowActions 포커스 규칙).
 */
export function DiscoverModelsDialog({
  provider,
  open,
  onOpenChange,
  returnFocusTo,
}: {
  provider: AdminProvider;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  returnFocusTo?: React.RefObject<HTMLElement | null>;
}) {
  // model_name -> 선택된 능력 목록 (키가 없으면 미선택)
  const [selected, setSelected] = useState<Record<string, string[]>>({});
  // 목록 필터 — 비어 있으면 전체 표시. 필터로 가려진 모델의 선택은 유지된다.
  const [modalityFilter, setModalityFilter] = useState<string[]>([]);

  const discover = useProviderAvailableModels(provider.id, open);
  const importModels = useImportModels(provider.id);

  const toggle = (m: DiscoveredModel) =>
    setSelected((prev) => {
      const next = { ...prev };
      if (m.model_name in next) delete next[m.model_name];
      else next[m.model_name] = m.capabilities;
      return next;
    });

  const toggleModality = (modality: string) =>
    setModalityFilter((prev) =>
      prev.includes(modality) ? prev.filter((x) => x !== modality) : [...prev, modality],
    );

  const chosen = (discover.data ?? []).filter((m) => m.model_name in selected);
  const modalityCounts = countModalities(discover.data ?? []);
  const visible = filterByModalities(discover.data ?? [], modalityFilter);

  function submit() {
    importModels.mutate(
      chosen.map((m) => ({
        model_name: m.model_name,
        display_name: m.display_name,
        capabilities: selected[m.model_name] ?? m.capabilities,
      })),
      {
        onSuccess: () => {
          setSelected({});
          onOpenChange(false);
        },
      },
    );
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o);
        if (!o) {
          setSelected({});
          setModalityFilter([]);
        }
      }}
    >
      <DialogContent className="grid-cols-1 sm:max-w-2xl" finalFocus={returnFocusTo}>
        <DialogHeader>
          <DialogTitle>{provider.name} — 모델 가져오기</DialogTitle>
        </DialogHeader>
        {discover.isLoading && (
          <p className="py-8 text-center text-sm text-muted-foreground">
            모델 목록을 불러오는 중…
          </p>
        )}
        {discover.isError && (
          <p className="py-8 text-center text-sm text-destructive">{discover.error.message}</p>
        )}
        {discover.data && discover.data.length === 0 && (
          <p className="py-8 text-center text-sm text-muted-foreground">
            제공되는 모델이 없습니다.
          </p>
        )}
        {discover.data && discover.data.length > 0 && (
          <>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="능력 필터">
              <Button
                size="xs"
                variant={modalityFilter.length === 0 ? "default" : "outline"}
                aria-pressed={modalityFilter.length === 0}
                onClick={() => setModalityFilter([])}
              >
                전체 {discover.data.length}
              </Button>
              {modalityCounts.map(({ modality, count }) => {
                const active = modalityFilter.includes(modality);
                return (
                  <Button
                    key={modality}
                    size="xs"
                    variant={active ? "default" : "outline"}
                    aria-pressed={active}
                    onClick={() => toggleModality(modality)}
                  >
                    {capabilityLabel(modality)} {count}
                  </Button>
                );
              })}
            </div>
            {/* 높이 고정 — 필터로 목록이 줄어도 모달 위치가 흔들리지 않게 */}
            <ScrollArea className="h-[55vh] pr-3">
              <div className="flex flex-col gap-1">
                {visible.map((m) => {
                  const isSelected = m.model_name in selected;
                  return (
                    <div
                      key={m.model_name}
                      className="flex items-center gap-3 rounded-md px-2 py-2 hover:bg-muted/50"
                    >
                      <Checkbox
                        aria-label={`${m.model_name} 선택`}
                        checked={isSelected}
                        disabled={m.already_registered}
                        onCheckedChange={() => toggle(m)}
                      />
                      <div
                        className={`min-w-0 flex-1 ${m.already_registered ? "" : "cursor-pointer"}`}
                        onClick={() => !m.already_registered && toggle(m)}
                      >
                        <div className="truncate font-mono text-xs">{m.model_name}</div>
                        <div className="truncate text-xs text-muted-foreground">{m.raw_id}</div>
                      </div>
                      {isSelected ? (
                        <div className="max-w-[60%]">
                          <CapabilityPicker
                            value={selected[m.model_name]}
                            onChange={(caps) =>
                              setSelected((p) => ({ ...p, [m.model_name]: caps }))
                            }
                          />
                        </div>
                      ) : m.already_registered ? (
                        <Badge variant="secondary" className="shrink-0">
                          등록됨
                        </Badge>
                      ) : (
                        <div className="flex shrink-0 flex-wrap justify-end gap-1">
                          {m.capabilities.map((c) => (
                            <Badge key={c} variant="outline">
                              {capabilityLabel(c)}
                            </Badge>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </ScrollArea>
            <Button onClick={submit} disabled={chosen.length === 0 || importModels.isPending}>
              {chosen.length}개 등록
            </Button>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
