"use client";

import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { SlideView } from "@/components/agents/slide-deck/slide-view";
import type { SlideImages } from "@/hooks/use-slide-images";
import type { SlideDeckDoc } from "@/lib/slide-deck/parse";

/** 인쇄 중 표식 — globals.css가 이 클래스가 있을 때만 인쇄용 덱 밖을 숨긴다. */
export const SLIDE_PRINTING_CLASS = "slide-printing";

/** 인쇄용 덱의 그림이 다 그려질 때까지 기다린다 — 실패한 그림은 기다리지 않는다(그 장은 이미지 없이 인쇄). */
async function imagesDecoded(root: HTMLElement | null): Promise<void> {
  const images = Array.from(root?.querySelectorAll("img") ?? []);
  await Promise.allSettled(images.map((img) => (typeof img.decode === "function" ? img.decode() : undefined)));
}

/**
 * PDF 인쇄(D-11) — `body` 바로 아래에 인쇄용 덱(한 장에 한 슬라이드, 13.333in × 7.5in)을 붙이고
 * `window.print()`를 부른다. 화면에서는 숨어 있다. 인쇄가 끝나면(`afterprint`) `onDone`으로 걷는다.
 * 의존성 없이 브라우저 인쇄 대화상자의 「PDF로 저장」을 쓴다. 삽화(core-v0.23.0-upgrade AC-10)가 있으면 그림을
 * 디코드한 다음 프레임에 인쇄한다 — 디코드 전에 인쇄하면 빈 칸으로 나간다.
 */
export function SlidePrint({
  doc,
  images,
  onDone,
}: {
  doc: SlideDeckDoc;
  images?: SlideImages;
  onDone: () => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const html = document.documentElement;
    html.classList.add(SLIDE_PRINTING_CLASS);
    window.addEventListener("afterprint", onDone);
    let cancelled = false;
    let frame = 0;
    // 인쇄용 덱이 그려진 다음 프레임에 인쇄한다
    const first = requestAnimationFrame(() => {
      void imagesDecoded(rootRef.current).then(() => {
        if (!cancelled) frame = requestAnimationFrame(() => window.print());
      });
    });
    return () => {
      cancelled = true;
      cancelAnimationFrame(first);
      cancelAnimationFrame(frame);
      window.removeEventListener("afterprint", onDone);
      html.classList.remove(SLIDE_PRINTING_CLASS);
    };
  }, [onDone]);

  return createPortal(
    <div ref={rootRef} data-slide-print-root aria-hidden>
      {doc.slides.map((slide, i) => (
        <div key={i} className="slide-print-page">
          <SlideView slide={slide} index={i} total={doc.slides.length} image={images?.get(i)} />
        </div>
      ))}
    </div>,
    document.body,
  );
}
