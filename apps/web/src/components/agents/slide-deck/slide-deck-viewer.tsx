"use client";

import { FileDown, Maximize2, Play, Printer } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { toast } from "sonner";
import { SlideDeckCoverage, SlideSourcesLine } from "@/components/agents/slide-deck/slide-coverage";
import { SlidePresenter, slideKeyTarget } from "@/components/agents/slide-deck/slide-presenter";
import { SlidePrint } from "@/components/agents/slide-deck/slide-print";
import { SlideView } from "@/components/agents/slide-deck/slide-view";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { type SlideImages, useSlideImages } from "@/hooks/use-slide-images";
import type { AgentRun } from "@/lib/agent-types";
import { boundedPrettyJson } from "@/lib/mindmap/bounded";
import { parseSlideDeck, type SlideDeckDoc } from "@/lib/slide-deck/parse";
import { downloadSlideDeckPptx } from "@/lib/slide-deck/pptx";
import { cn } from "@/lib/utils";

const FORMAT_LABEL = { detailed: "자세한 자료", presenter: "발표자 슬라이드" } as const;

/**
 * 슬라이드 자료 뷰어(docs/plans/slide-deck-studio.md AC-7~11) — 패널 안에서는 슬라이드를 세로로 나열하고,
 * 위 도구 막대에서 발표 모드·확대 보기·PDF 인쇄·PPTX 다운로드를 연다. 형식이 맞지 않으면 안내와 함께 원문 JSON으로 대체한다.
 * 덱 run의 삽화 산출물(core-v0.23.0-upgrade AC-10)은 목록·확대·발표·인쇄·PPTX의 같은 장에 같은 위치로 붙는다.
 * 덱의 소스 반영 표시(#218 — 대체 본문·미반영 소스·만들지 못한 장, 장마다 근거 소스)는 목록 위와 슬라이드 아래에 둔다.
 */
export function SlideDeckViewer({ content, run }: { content: string; run?: AgentRun }) {
  const parsed = useMemo(() => parseSlideDeck(content), [content]);
  if (!parsed.ok) {
    return (
      <div className="flex flex-col gap-2" data-testid="slide-deck-fallback">
        <p className="text-sm text-muted-foreground">
          슬라이드 자료 형식이 아니어서 원문을 보여 줍니다
          {parsed.reason === "invalid_shape" ? ` (${parsed.detail})` : ""}.
        </p>
        <pre className="text-xs whitespace-pre-wrap">
          {"preview" in parsed ? parsed.preview : boundedPrettyJson(content)}
        </pre>
      </div>
    );
  }
  return <SlideDeck doc={parsed.doc} run={run} />;
}

type Mode = { kind: "list" } | { kind: "expanded"; index: number } | { kind: "present"; index: number; back: "list" | "expanded" };

function SlideDeck({ doc, run }: { doc: SlideDeckDoc; run?: AgentRun }) {
  const [mode, setMode] = useState<Mode>({ kind: "list" });
  const [printing, setPrinting] = useState(false);
  const total = doc.slides.length;
  const { images, loading: imagesLoading } = useSlideImages(run, total);
  const stopPrinting = useCallback(() => setPrinting(false), []);
  const pptx = usePptxDownload(doc, images);
  const endPresent = useCallback(
    (last: number) =>
      setMode((m) => (m.kind === "present" && m.back === "expanded" ? { kind: "expanded", index: last } : { kind: "list" })),
    [],
  );

  return (
    <div className="flex flex-col gap-3" data-testid="slide-deck-viewer">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-sm font-semibold" title={doc.title}>
            {doc.title}
          </h3>
          <p className="text-xs text-muted-foreground">
            {FORMAT_LABEL[doc.format]} · 슬라이드 {total}장
          </p>
        </div>
        <DeckActions
          onPresent={() => setMode({ kind: "present", index: 0, back: "list" })}
          onExpand={() => setMode({ kind: "expanded", index: 0 })}
          onPrint={() => setPrinting(true)}
          pptx={pptx}
          exportWaiting={imagesLoading}
        />
      </div>
      {doc.truncated && (
        <p className="text-xs text-muted-foreground">슬라이드가 많아 앞의 {total}장만 보여 줍니다.</p>
      )}
      {doc.coverage && <SlideDeckCoverage coverage={doc.coverage} />}
      <ol className="flex flex-col gap-3" aria-label={`${doc.title} 슬라이드 목록`}>
        {doc.slides.map((slide, i) => (
          <li key={i} className="flex flex-col gap-1">
            <button
              type="button"
              aria-label={`슬라이드 ${i + 1} 크게 보기`}
              onClick={() => setMode({ kind: "expanded", index: i })}
              className="overflow-hidden rounded-md ring-1 ring-border outline-none hover:ring-primary/60 focus-visible:ring-2 focus-visible:ring-ring"
            >
              <SlideView slide={slide} index={i} total={total} image={images.get(i)} />
            </button>
            <SlideSourcesLine sources={doc.coverage?.bySlide[i]} />
            {slide.notes && (
              <details className="px-1 text-xs text-muted-foreground">
                <summary className="cursor-pointer select-none">발표 노트</summary>
                <p className="mt-1 leading-relaxed whitespace-pre-wrap">{slide.notes}</p>
              </details>
            )}
          </li>
        ))}
      </ol>

      <SlideDeckExpanded
        doc={doc}
        images={images}
        index={mode.kind === "expanded" ? mode.index : null}
        onIndex={(index) => setMode({ kind: "expanded", index })}
        onClose={() => setMode({ kind: "list" })}
        onPresent={(index) => setMode({ kind: "present", index, back: "expanded" })}
        onPrint={() => setPrinting(true)}
        pptx={pptx}
        exportWaiting={imagesLoading}
      />
      {mode.kind === "present" && (
        <SlidePresenter doc={doc} images={images} start={mode.index} onClose={endPresent} />
      )}
      {printing && <SlidePrint doc={doc} images={images} onDone={stopPrinting} />}
    </div>
  );
}

/** 도구 막대 아이콘 버튼 — 터치 기기(`pointer: coarse`)에서는 44px 타깃(ui-ux-redesign plan DP-6). */
const TOUCH = "coarse:size-11";

type PptxDownload = { pending: boolean; start: () => void };

/** PPTX 다운로드(AC-11) — 라이브러리 로드·생성 중에는 버튼을 끄고, 실패하면 토스트로 알린다. 삽화는 지금 보이는 것을 넣는다. */
function usePptxDownload(doc: SlideDeckDoc, images: SlideImages): PptxDownload {
  const [pending, setPending] = useState(false);
  const start = useCallback(() => {
    setPending(true);
    downloadSlideDeckPptx(doc, images)
      .catch(() => toast.error("PPTX 파일을 만들지 못했습니다"))
      .finally(() => setPending(false));
  }, [doc, images]);
  return { pending, start };
}

function DeckActions({
  onPresent,
  onExpand,
  onPrint,
  pptx,
  exportWaiting,
}: {
  onPresent: () => void;
  onExpand?: () => void;
  onPrint: () => void;
  pptx: PptxDownload;
  /** 삽화를 받는 중 — 인쇄·PPTX를 잠시 끈다(그림이 빠진 파일 방지, PR-2 리뷰 M2). */
  exportWaiting: boolean;
}) {
  const waitTitle = exportWaiting ? " — 삽화를 불러오는 중입니다" : "";
  return (
    <div className="flex shrink-0 items-center gap-1">
      <Button variant="ghost" size="icon-sm" className={TOUCH} aria-label="발표 시작" title="발표 시작" onClick={onPresent}>
        <Play />
      </Button>
      {onExpand && (
        <Button variant="ghost" size="icon-sm" className={TOUCH} aria-label="크게 보기" title="크게 보기" onClick={onExpand}>
          <Maximize2 />
        </Button>
      )}
      <Button
        variant="ghost"
        size="icon-sm"
        className={TOUCH}
        aria-label="PDF로 인쇄"
        title={`PDF로 인쇄${waitTitle}`}
        disabled={exportWaiting}
        onClick={onPrint}
      >
        <Printer />
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        className={TOUCH}
        aria-label="PPTX 다운로드"
        title={`PPTX 다운로드${waitTitle}`}
        disabled={pptx.pending || exportWaiting}
        onClick={pptx.start}
      >
        <FileDown />
      </Button>
    </div>
  );
}

/** 확대 보기(AC-8) — 본문 슬라이드와 오른쪽 번호 썸네일. ←/→ 등으로 넘긴다. */
function SlideDeckExpanded({
  doc,
  images,
  index,
  onIndex,
  onClose,
  onPresent,
  onPrint,
  pptx,
  exportWaiting,
}: {
  doc: SlideDeckDoc;
  images: SlideImages;
  exportWaiting: boolean;
  index: number | null;
  onIndex: (index: number) => void;
  onClose: () => void;
  onPresent: (index: number) => void;
  onPrint: () => void;
  pptx: PptxDownload;
}) {
  const total = doc.slides.length;
  const current = index ?? 0;
  const slide = doc.slides[current];

  function onKeyDown(e: React.KeyboardEvent) {
    const next = slideKeyTarget(e.key, current, total);
    // 텍스트·버튼 위의 Space는 그 요소의 동작으로 남긴다
    if (next === null || (e.key === " " && e.target !== e.currentTarget)) return;
    e.preventDefault();
    onIndex(next);
  }

  return (
    <Dialog open={index !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        onKeyDown={onKeyDown}
        className="flex h-[92dvh] flex-col gap-3 sm:max-w-[min(96vw,1400px)]"
        data-testid="slide-deck-expanded"
      >
        <div className="flex items-center gap-2 pr-8">
          <div className="min-w-0 flex-1">
            <DialogTitle className="truncate">{doc.title}</DialogTitle>
            <DialogDescription className="text-xs">
              슬라이드 {current + 1} / {total}
            </DialogDescription>
          </div>
          <DeckActions
            onPresent={() => onPresent(current)}
            onPrint={onPrint}
            pptx={pptx}
            exportWaiting={exportWaiting}
          />
        </div>
        <div className="flex min-h-0 flex-1 gap-3">
          <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2 overflow-y-auto">
            <div className="overflow-hidden rounded-md ring-1 ring-border" data-testid="slide-deck-expanded-main">
              <SlideView slide={slide} index={current} total={total} image={images.get(current)} />
            </div>
            <SlideSourcesLine sources={doc.coverage?.bySlide[current]} />
            {slide.notes && (
              <p className="rounded-md bg-muted/60 p-3 text-sm leading-relaxed whitespace-pre-wrap">
                <span className="mb-1 block text-xs font-semibold text-muted-foreground">발표 노트</span>
                {slide.notes}
              </p>
            )}
          </div>
          <ol
            aria-label="슬라이드 썸네일"
            className="hidden w-36 shrink-0 flex-col gap-2 overflow-y-auto pr-1 sm:flex lg:w-44"
          >
            {doc.slides.map((s, i) => (
              <li key={i} className="flex items-start gap-1.5">
                <span className="w-4 shrink-0 pt-1 text-right text-xs tabular-nums text-muted-foreground">{i + 1}</span>
                <button
                  type="button"
                  aria-label={`슬라이드 ${i + 1}로 이동`}
                  aria-current={i === current ? "true" : undefined}
                  onClick={() => onIndex(i)}
                  className={cn(
                    "min-w-0 flex-1 overflow-hidden rounded-sm ring-1 ring-border outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    i === current && "ring-2 ring-primary",
                  )}
                >
                  <SlideView slide={s} index={i} total={total} image={images.get(i)} />
                </button>
              </li>
            ))}
          </ol>
        </div>
      </DialogContent>
    </Dialog>
  );
}
