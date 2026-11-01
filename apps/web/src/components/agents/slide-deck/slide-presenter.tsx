"use client";

import { ChevronLeft, ChevronRight, NotebookText, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { SlideView } from "@/components/agents/slide-deck/slide-view";
import type { SlideImages } from "@/hooks/use-slide-images";
import type { SlideDeckDoc } from "@/lib/slide-deck/parse";

/** 덱 넘김 키(D-10) — 다음·이전·처음·끝. 그 밖의 키는 null. */
export function slideKeyTarget(key: string, current: number, total: number): number | null {
  switch (key) {
    case "ArrowRight":
    case "ArrowDown":
    case "PageDown":
    case " ":
      return Math.min(current + 1, total - 1);
    case "ArrowLeft":
    case "ArrowUp":
    case "PageUp":
      return Math.max(current - 1, 0);
    case "Home":
      return 0;
    case "End":
      return total - 1;
    default:
      return null;
  }
}

/** 포커스가 버튼 같은 조작 요소에 있으면 Space·Enter는 그 요소의 기본 동작으로 남긴다. */
function isControl(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && target.closest("button, a, input, textarea, select") !== null;
}

/**
 * 발표 모드(D-10) — 화면 전체를 덮는 검은 무대에 슬라이드 한 장. Fullscreen API가 있으면 전체 화면으로
 * 들어가고(없으면 고정 오버레이로 대신한다), ←/→/PageUp/PageDown/Space/Home/End로 넘기고 Esc로 끝낸다.
 * N은 발표 노트를 켜고 끈다. 전체 화면을 브라우저가 먼저 끝내면(Esc) 발표도 끝난다.
 * 키는 window에서 받는다 — 포커스가 어디로 가도 Esc가 닿는다. Tab은 무대 안에서 돌고, 끝나면 연 요소로
 * 포커스를 돌려준다(독립 리뷰 지적 — 오버레이 대체 경로의 포커스 이탈·버튼 Space 가로채기).
 */
export function SlidePresenter({
  doc,
  images,
  start,
  onClose,
}: {
  doc: SlideDeckDoc;
  /** 장 위치 → 삽화 URL(core-v0.23.0-upgrade AC-10). */
  images?: SlideImages;
  start: number;
  onClose: (last: number) => void;
}) {
  const total = doc.slides.length;
  const [index, setIndex] = useState(start);
  const [notes, setNotes] = useState(false);
  const stageRef = useRef<HTMLDivElement>(null);
  const indexRef = useRef(index);
  useEffect(() => {
    indexRef.current = index;
  }, [index]);

  const close = useCallback(() => {
    if (typeof document !== "undefined" && document.fullscreenElement) {
      void document.exitFullscreen?.().catch(() => undefined);
    }
    onClose(indexRef.current);
  }, [onClose]);

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const stage = stageRef.current;
    stage?.focus();
    let entered = false;
    // 전체 화면 거부(권한·iframe)는 오버레이로 계속한다
    stage
      ?.requestFullscreen?.()
      .then(() => {
        entered = true;
      })
      .catch(() => undefined);
    function onFullscreenChange() {
      if (entered && !document.fullscreenElement) onClose(indexRef.current);
    }
    document.addEventListener("fullscreenchange", onFullscreenChange);
    return () => {
      document.removeEventListener("fullscreenchange", onFullscreenChange);
      // 연 요소가 아직 문서에 있으면 포커스를 돌려준다(확대 보기에서 열었으면 그 창은 이미 닫혔다)
      if (opener?.isConnected) opener.focus();
    };
  }, [onClose]);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      const stage = stageRef.current;
      if (!stage) return;
      if (e.key === "Tab") {
        // 무대 밖으로 나가지 않게 조작 버튼 사이에서 돈다
        const focusables = Array.from(stage.querySelectorAll<HTMLElement>("button:not([disabled])"));
        e.preventDefault();
        if (focusables.length === 0) {
          stage.focus();
          return;
        }
        const at = focusables.indexOf(document.activeElement as HTMLElement);
        const next = e.shiftKey
          ? at <= 0
            ? focusables.length - 1
            : at - 1
          : at === -1 || at === focusables.length - 1
            ? 0
            : at + 1;
        focusables[next].focus();
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        close();
        return;
      }
      if ((e.key === " " || e.key === "Enter") && isControl(e.target)) return;
      if (e.key === "n" || e.key === "N") {
        e.preventDefault();
        setNotes((v) => !v);
        return;
      }
      const next = slideKeyTarget(e.key, indexRef.current, total);
      if (next !== null) {
        e.preventDefault();
        setIndex(next);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [close, total]);

  const slide = doc.slides[index];
  return createPortal(
    <div
      ref={stageRef}
      role="dialog"
      aria-modal="true"
      aria-label={`${doc.title} 발표`}
      tabIndex={-1}
      className="group fixed inset-0 z-[60] flex flex-col items-center justify-center bg-black outline-none"
      data-testid="slide-presenter"
    >
      <div className="w-[min(100vw,calc(100dvh*16/9))]">
        <SlideView slide={slide} index={index} total={total} image={images?.get(index)} />
      </div>
      {notes && (
        <div
          className="absolute inset-x-0 bottom-0 max-h-[30vh] overflow-y-auto bg-black/85 px-8 py-4 text-base leading-relaxed text-white"
          data-testid="slide-presenter-notes"
        >
          {slide.notes ?? "이 슬라이드에는 발표 노트가 없습니다."}
        </div>
      )}
      <div className="absolute top-3 right-3 flex gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
        <PresenterButton label={notes ? "발표 노트 숨기기 (N)" : "발표 노트 보기 (N)"} onClick={() => setNotes((v) => !v)}>
          <NotebookText className="size-5" />
        </PresenterButton>
        <PresenterButton label="이전 슬라이드" onClick={() => setIndex((i) => Math.max(i - 1, 0))} disabled={index === 0}>
          <ChevronLeft className="size-5" />
        </PresenterButton>
        <PresenterButton
          label="다음 슬라이드"
          onClick={() => setIndex((i) => Math.min(i + 1, total - 1))}
          disabled={index === total - 1}
        >
          <ChevronRight className="size-5" />
        </PresenterButton>
        <PresenterButton label="발표 끝내기 (Esc)" onClick={close}>
          <X className="size-5" />
        </PresenterButton>
      </div>
    </div>,
    document.body,
  );
}

function PresenterButton({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      disabled={disabled}
      className="rounded-md bg-white/10 p-2 text-white outline-none hover:bg-white/20 focus-visible:ring-2 focus-visible:ring-white disabled:opacity-40"
    >
      {children}
    </button>
  );
}
