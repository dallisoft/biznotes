"use client";

import { Radio } from "@base-ui/react/radio";
import { RadioGroup } from "@base-ui/react/radio-group";
import { ArrowLeft, Check, ChevronRight, Presentation } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { RunModelSelect } from "@/components/agents/run-model-select";
import { SourcePicker } from "@/components/agents/source-picker";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useCreateRun } from "@/hooks/use-agents";
import { useImageModels } from "@/hooks/use-chat";
import { useSources } from "@/hooks/use-sources";
import { useStudioCatalog } from "@/hooks/use-studio";
import {
  DEFAULT_SLIDE_PARAMS,
  SLIDE_DESCRIPTION_MAX,
  SLIDE_IMAGE_MAX,
  type SlideCommonParams,
  type SlideLanguage,
  type SlideLength,
} from "@/lib/slide-deck/flow";
import type { SlideFormat } from "@/lib/slide-deck/parse";
import {
  findStudioAgent,
  SLIDE_DECK_CARD_ID,
  SLIDE_OUTLINE_CARD_ID,
  studioCardById,
} from "@/lib/studio-registry";
import { cn } from "@/lib/utils";

const FORMATS: { value: SlideFormat; label: string; description: string }[] = [
  {
    value: "detailed",
    label: "자세한 자료",
    description: "전체 텍스트와 세부정보가 포함된 포괄적인 자료로, 이메일로 보내거나 단독으로 읽기에 적합합니다.",
  },
  {
    value: "presenter",
    label: "발표자 슬라이드",
    description: "발표하는 동안 도움이 될 핵심 내용을 담은 깔끔하고 시각적인 슬라이드입니다.",
  },
];

const LENGTHS = [
  { value: "short", label: "짧게" },
  { value: "default", label: "기본값" },
] as const satisfies readonly { value: SlideLength; label: string }[];

const LANGUAGES = [
  { value: "한국어", label: "한국어" },
  { value: "English", label: "English" },
] as const satisfies readonly { value: SlideLanguage; label: string }[];

/**
 * 「슬라이드 자료 맞춤설정」 다이얼로그(docs/plans/slide-deck-studio.md AC-2·AC-3·AC-4).
 * 형식·언어·길이·소스·설명을 고르고, 「슬라이드 개요 검토」를 켜면 개요 run(「슬라이드 자료 개요」)을, 끄면 덱 run
 * (「슬라이드 자료」)을 만든다. 소스는 하위 화면에서 고른다 — 선택 없음은 노트북 전체다.
 * 「이미지 넣기」(core-v0.23.0-upgrade AC-8·AC-11)는 이미지 생성 모델이 있을 때만 켤 수 있고, 켜면 삽화 장수
 * `SLIDE_IMAGE_MAX`를 보낸다. 개요 run도 장수를 받아 두어 「계속」이 그대로 넘긴다.
 * 「모델」(#216)은 기본값 「기본 모델」이면 `model_id`를 보내지 않는다. 고른 모델은 개요 run의 `model_id`로 남아
 * 「계속」이 덱 run에도 같은 모델을 쓴다. 소스 요약도 고른 모델로 만든다(코어 #396).
 * 「고른 모델로 요약 다시 만들기」(#239)는 다른 모델이 만든 요약 캐시를 고른 모델로 다시 만든다. 켰을 때만
 * `refresh_digest: true`를 보낸다 — 시드 갱신 전 정의는 이 키를 모른다. 「계속」은 넘기지 않는다(개요 run이 이미
 * 다시 만들었다, core-v0.27.0-upgrade D3).
 */
export function SlideDeckDialog({
  notebookId,
  open,
  onOpenChange,
  initialSourceIds,
}: {
  notebookId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 소스 선택 기본값(현재 소스 패널 선택). 빈 배열 = 노트북 전체. */
  initialSourceIds: string[];
}) {
  const studio = useStudioCatalog();
  const createRun = useCreateRun(notebookId);
  const sources = useSources(notebookId);
  const imageModels = useImageModels();
  const imagesAvailable = (imageModels.data ?? []).length > 0;
  const [params, setParams] = useState<SlideCommonParams>(DEFAULT_SLIDE_PARAMS);
  const [review, setReview] = useState(false);
  const [sourceIds, setSourceIds] = useState<string[]>([]);
  // 실행 모델(#216) — null = 「기본 모델」. 개요 run이 받아 두면 「계속」이 덱 run에 그대로 넘긴다
  const [modelId, setModelId] = useState<string | null>(null);
  const [refreshDigest, setRefreshDigest] = useState(false);
  const [view, setView] = useState<"main" | "sources">("main");

  useEffect(() => {
    if (open) {
      setParams(DEFAULT_SLIDE_PARAMS);
      setReview(false);
      setSourceIds(initialSourceIds);
      setModelId(null);
      setRefreshDigest(false);
      setView("main");
    }
    // 정당: 여는 시점의 기본값만 쓴다 — 소스 패널 선택이 바뀌어도 열린 폼을 덮지 않는다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const deckAgent = findStudioAgent(studioCardById(SLIDE_DECK_CARD_ID), studio.catalog);
  const outlineAgent = findStudioAgent(studioCardById(SLIDE_OUTLINE_CARD_ID), studio.catalog);
  const agent = review ? outlineAgent : deckAgent;
  const readyCount = (sources.data ?? []).filter((s) => s.status === "ready").length;
  const sourceLabel = sourceIds.length > 0 ? `소스 ${sourceIds.length}개` : `전체 소스 ${readyCount}개`;

  function set<K extends keyof SlideCommonParams>(key: K, value: SlideCommonParams[K]) {
    setParams((p) => ({ ...p, [key]: value }));
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!agent) return;
    createRun.mutate(
      {
        agent_id: agent.id,
        params: {
          ...params,
          description: params.description.trim(),
          image_count: imagesAvailable ? params.image_count : 0,
          ...(refreshDigest ? { refresh_digest: true } : {}),
        },
        ...(sourceIds.length > 0 ? { source_ids: sourceIds } : {}),
        ...(modelId ? { model_id: modelId } : {}),
      },
      {
        onSuccess: () => {
          toast.success(
            review
              ? "슬라이드 개요를 만들고 있습니다. 완료되면 결과에서 검토하세요"
              : "슬라이드 자료 생성을 시작했습니다",
          );
          onOpenChange(false);
        },
        onError: (err) => toast.error(`실행 실패: ${err.message}`),
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92dvh] overflow-y-auto sm:max-w-2xl" data-testid="slide-deck-dialog">
        {view === "sources" ? (
          <>
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label="맞춤설정으로 돌아가기"
                  onClick={() => setView("main")}
                >
                  <ArrowLeft />
                </Button>
                소스
              </DialogTitle>
              <DialogDescription>슬라이드 자료에 쓸 소스를 고릅니다.</DialogDescription>
            </DialogHeader>
            <SourcePicker notebookId={notebookId} selected={sourceIds} onChange={setSourceIds} />
            <DialogFooter>
              <Button type="button" onClick={() => setView("main")} data-testid="slide-deck-sources-done">
                확인
              </Button>
            </DialogFooter>
          </>
        ) : (
          <form onSubmit={submit} className="flex min-w-0 flex-col gap-5">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <Presentation className="size-4" aria-hidden /> 슬라이드 자료 맞춤설정
              </DialogTitle>
              <DialogDescription>노트북 소스로 자세한 자료나 발표용 슬라이드를 만듭니다.</DialogDescription>
            </DialogHeader>

            <fieldset className="flex flex-col gap-2">
              <legend className="mb-2 text-sm font-medium">형식</legend>
              <RadioGroup
                aria-label="형식"
                value={params.format}
                onValueChange={(v) => set("format", v as SlideFormat)}
                className="grid gap-2 sm:grid-cols-2"
              >
                {FORMATS.map((f) => (
                  <Radio.Root
                    key={f.value}
                    value={f.value}
                    data-testid={`slide-format-${f.value}`}
                    className="group flex cursor-pointer flex-col gap-1.5 rounded-xl border p-3 text-left outline-none transition-colors hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-ring/50 data-checked:border-primary/40 data-checked:bg-primary/10"
                  >
                    <span className="flex items-center justify-between gap-2 text-sm font-semibold">
                      {f.label}
                      <span
                        aria-hidden
                        className="flex size-5 items-center justify-center rounded-full border bg-background group-data-checked:border-primary group-data-checked:bg-primary group-data-checked:text-primary-foreground"
                      >
                        <Check className="size-3 opacity-0 group-data-checked:opacity-100" />
                      </span>
                    </span>
                    <span className="text-xs leading-relaxed text-muted-foreground">{f.description}</span>
                  </Radio.Root>
                ))}
              </RadioGroup>
            </fieldset>

            <div className="flex flex-wrap items-end gap-x-6 gap-y-4">
              <div className="flex flex-col gap-2">
                <span className="text-sm font-medium">언어</span>
                <SegmentedControl
                  label="언어"
                  options={LANGUAGES}
                  value={params.language}
                  onValueChange={(v) => set("language", v)}
                />
              </div>
              <div className="flex flex-col gap-2">
                <span className="text-sm font-medium">길이</span>
                <SegmentedControl
                  label="길이"
                  options={LENGTHS}
                  value={params.length}
                  onValueChange={(v) => set("length", v)}
                />
              </div>
              <div className="flex flex-col gap-2">
                <span className="text-sm font-medium">소스</span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setView("sources")}
                  aria-label={`소스 선택 — 현재 ${sourceLabel}`}
                  data-testid="slide-deck-sources"
                >
                  {sourceLabel}
                  <ChevronRight />
                </Button>
              </div>
              <RunModelSelect value={modelId} onChange={setModelId} />
            </div>

            <div className="flex flex-col gap-2">
              <label htmlFor="slide-deck-description" className="text-sm font-medium">
                만들려는 슬라이드 자료에 대한 설명
              </label>
              <Textarea
                id="slide-deck-description"
                value={params.description}
                maxLength={SLIDE_DESCRIPTION_MAX}
                onChange={(e) => set("description", e.target.value)}
                rows={3}
                placeholder={
                  '간략한 개요를 추가하거나 청중, 스타일, 강조할 점에 대한 가이드 제공: "단계별 안내에 중점을 둔 대담하고 재미있는 스타일의 초보자용 자료 만들어 줘."'
                }
                data-testid="slide-deck-description"
              />
            </div>

            <div className="flex items-center justify-between gap-4">
              <span className="flex flex-col gap-0.5">
                <span id="slide-deck-images-label" className="text-sm font-medium">
                  이미지 넣기
                </span>
                <span id="slide-deck-images-help" className="text-xs text-muted-foreground">
                  {imagesAvailable
                    ? `그림이 도움이 되는 슬라이드에 AI 삽화를 최대 ${SLIDE_IMAGE_MAX}장 넣습니다. 만드는 시간이 늘어납니다.`
                    : imageModels.isPending
                      ? "이미지 생성 모델을 확인하고 있습니다."
                      : "이미지 생성 모델이 없어 켤 수 없습니다. 관리자가 이미지 모델을 등록하면 쓸 수 있습니다."}
                </span>
              </span>
              <Switch
                aria-labelledby="slide-deck-images-label"
                aria-describedby="slide-deck-images-help"
                checked={imagesAvailable && params.image_count > 0}
                disabled={!imagesAvailable}
                onCheckedChange={(on) => set("image_count", on ? SLIDE_IMAGE_MAX : 0)}
                data-testid="slide-deck-images"
              />
            </div>

            <div className="flex items-center justify-between gap-4">
              <span className="flex flex-col gap-0.5">
                <span id="slide-deck-refresh-digest-label" className="text-sm font-medium">
                  고른 모델로 요약 다시 만들기
                </span>
                <span id="slide-deck-refresh-digest-help" className="text-xs text-muted-foreground">
                  다른 모델이 만들어 둔 소스 요약을 위에서 고른 모델로 다시 만듭니다. 요약 품질을 비교할 때 쓰고, 시간과
                  토큰이 더 듭니다. 끄면 만들어 둔 요약을 그대로 씁니다.
                </span>
              </span>
              <Switch
                aria-labelledby="slide-deck-refresh-digest-label"
                aria-describedby="slide-deck-refresh-digest-help"
                checked={refreshDigest}
                onCheckedChange={setRefreshDigest}
                data-testid="slide-deck-refresh-digest"
              />
            </div>

            <div className="flex items-center justify-between gap-4">
              <span className="flex flex-col gap-0.5">
                <span id="slide-deck-review-label" className="text-sm font-medium">
                  슬라이드 개요 검토
                </span>
                <span id="slide-deck-review-help" className="text-xs text-muted-foreground">
                  슬라이드 자료를 만들기 전에 개요를 미리 보고 의견을 남길 수 있습니다. 개요가 준비되면 결과에
                  「검토」 버튼이 생깁니다.
                </span>
              </span>
              <Switch
                aria-labelledby="slide-deck-review-label"
                aria-describedby="slide-deck-review-help"
                checked={review}
                onCheckedChange={setReview}
                data-testid="slide-deck-review"
              />
            </div>

            {!studio.isPending && !agent && (
              <p className="text-xs text-destructive">
                {review ? "슬라이드 자료 개요" : "슬라이드 자료"} 에이전트를 찾을 수 없습니다
              </p>
            )}
            <DialogFooter>
              <Button
                type="submit"
                disabled={!agent || createRun.isPending}
                className={cn(createRun.isPending && "cursor-progress")}
                data-testid="slide-deck-generate"
              >
                {createRun.isPending ? "시작 중..." : review ? "개요 만들기" : "생성"}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
