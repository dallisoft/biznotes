import { Check } from "lucide-react";
import { useState } from "react";
import type { Slide } from "@/lib/slide-deck/parse";
import { cn } from "@/lib/utils";

/**
 * 슬라이드 한 장(docs/plans/slide-deck-studio.md D-7·D-9) — 16:9 컨테이너에 layout 8종을 그린다.
 * 글자 크기는 컨테이너 너비 단위(cqw)라 패널 인라인·확대 보기·발표·인쇄에서 비율이 같다.
 * 색은 앱 테마와 무관한 고정 「종이」 팔레트(`.slide-theme`)다. 본문은 텍스트 노드로만 그린다.
 * 버튼(목록·썸네일) 안에 놓여도 가운데 정렬을 물려받지 않게 왼쪽 정렬을 못 박는다.
 *
 * 삽화(core-v0.23.0-upgrade D3·AC-10): `image`가 있으면 오른쪽 40%(테두리 안쪽)에 그림을 채우고 본문은 왼쪽
 * 60%에 그린다 — layout 8종 공통이다. 그림을 불러오지 못하면 그 장은 이미지 없이 그린다. PPTX(`pptx.ts`)도 같은 칸을 쓴다.
 */
export function SlideView({
  slide,
  index,
  total,
  image,
  className,
}: {
  slide: Slide;
  /** 0부터 — 오른쪽 아래 쪽 번호와 접근 가능한 이름에 쓴다. */
  index: number;
  total: number;
  /** 삽화 object URL(없으면 이미지 없이). */
  image?: string;
  className?: string;
}) {
  const [broken, setBroken] = useState<string | null>(null);
  const shown = image && image !== broken ? image : undefined;
  return (
    <div
      role="group"
      aria-roledescription="슬라이드"
      aria-label={`슬라이드 ${index + 1} / ${total}: ${slide.heading}`}
      data-layout={slide.layout}
      data-image={shown ? "true" : undefined}
      className={cn(
        "slide-theme @container relative aspect-video w-full overflow-hidden bg-slide-paper text-left text-slide-ink",
        className,
      )}
    >
      <div className="absolute inset-[2.2cqw] rounded-[0.6cqw] border-[0.15cqw] border-slide-rule" aria-hidden />
      {shown && (
        // 장식 그림 — 슬라이드 내용은 본문 텍스트가 전한다
        // eslint-disable-next-line @next/next/no-img-element -- 인증 요청으로 받은 blob URL이라 next/image 최적화를 쓰지 않는다
        <img
          src={shown}
          alt=""
          onError={() => setBroken(shown)}
          className="absolute top-[2.2cqw] right-[2.2cqw] bottom-[2.2cqw] left-[60cqw] h-[calc(100%-4.4cqw)] w-[37.8cqw] rounded-r-[0.6cqw] object-cover"
          data-testid="slide-image"
        />
      )}
      <div className={cn("relative flex h-full flex-col py-[5cqw] pl-[6cqw]", shown ? "pr-[43cqw]" : "pr-[6cqw]")}>
        <SlideBody slide={slide} />
      </div>
      <span
        className={cn(
          "absolute right-[3.2cqw] bottom-[2.8cqw] text-[1.3cqw] tabular-nums text-slide-muted",
          shown && "rounded-[0.4cqw] bg-slide-paper/85 px-[0.8cqw]",
        )}
      >
        {index + 1} / {total}
      </span>
    </div>
  );
}

function Heading({ children, className }: { children: string; className?: string }) {
  return (
    <h3 className={cn("text-[3.6cqw] leading-tight font-bold tracking-tight break-keep", className)}>
      {children}
    </h3>
  );
}

function Body({ text }: { text?: string }) {
  if (!text) return null;
  return <p className="mt-[1.4cqw] line-clamp-4 text-[1.75cqw] leading-relaxed text-slide-muted">{text}</p>;
}

function Bullets({ items, check = false }: { items: string[]; check?: boolean }) {
  if (items.length === 0) return null;
  return (
    <ul className="mt-[2cqw] flex flex-col gap-[1.2cqw] text-[1.9cqw] leading-snug">
      {items.map((b, i) => (
        <li key={i} className="flex items-start gap-[1.2cqw]">
          {check ? (
            <Check aria-hidden className="mt-[0.3cqw] size-[2cqw] shrink-0 text-slide-accent" />
          ) : (
            <span aria-hidden className="mt-[0.75cqw] size-[0.9cqw] shrink-0 rounded-full bg-slide-accent" />
          )}
          <span className="line-clamp-2">{b}</span>
        </li>
      ))}
    </ul>
  );
}

function SlideBody({ slide }: { slide: Slide }) {
  switch (slide.layout) {
    case "title":
      return (
        <div className="flex h-full flex-col justify-center">
          <span aria-hidden className="mb-[2.4cqw] h-[0.6cqw] w-[9cqw] rounded-full bg-slide-accent" />
          <h3 className="text-[5.4cqw] leading-[1.15] font-extrabold tracking-tight break-keep">{slide.heading}</h3>
          {slide.subheading && (
            <p className="mt-[2.4cqw] text-[2.4cqw] leading-snug font-semibold text-slide-warm">{slide.subheading}</p>
          )}
          <Body text={slide.body} />
        </div>
      );
    case "section":
      return (
        <div className="flex h-full items-center gap-[4cqw]">
          <span aria-hidden className="h-[22cqw] w-[1cqw] shrink-0 rounded-full bg-slide-warm" />
          <div>
            <h3 className="text-[4.6cqw] leading-tight font-bold break-keep">{slide.heading}</h3>
            {slide.subheading && (
              <p className="mt-[1.6cqw] text-[2.2cqw] text-slide-muted">{slide.subheading}</p>
            )}
            <Body text={slide.body} />
          </div>
        </div>
      );
    case "two_column":
      return (
        <>
          <Heading>{slide.heading}</Heading>
          <Body text={slide.body} />
          <div className="mt-[2.4cqw] grid min-h-0 grid-cols-2 gap-[2.4cqw]">
            {slide.columns.map((c, i) => (
              <section key={i} className="min-h-0 overflow-hidden rounded-[1cqw] bg-slide-panel p-[2.4cqw]">
                {c.heading && (
                  <h4 className={cn("text-[2.3cqw] font-bold", i === 0 ? "text-slide-accent" : "text-slide-warm")}>
                    {c.heading}
                  </h4>
                )}
                <Bullets items={c.bullets} />
              </section>
            ))}
          </div>
        </>
      );
    case "steps":
      return (
        <>
          <Heading>{slide.heading}</Heading>
          <Body text={slide.body} />
          <ol className="mt-[3cqw] flex min-h-0 items-stretch gap-[1.6cqw]">
            {slide.steps.map((s, i) => (
              <li key={i} className="flex min-w-0 flex-1 flex-col rounded-[1cqw] bg-slide-panel p-[1.8cqw]">
                <span className="flex size-[3.6cqw] items-center justify-center rounded-full bg-slide-accent text-[1.8cqw] font-bold text-slide-paper">
                  {i + 1}
                </span>
                <span className="mt-[1.4cqw] text-[2cqw] leading-snug font-bold break-keep">{s.label}</span>
                {s.detail && (
                  <span className="mt-[0.8cqw] line-clamp-5 text-[1.5cqw] leading-snug text-slide-muted">
                    {s.detail}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </>
      );
    case "stats":
      return (
        <>
          <Heading>{slide.heading}</Heading>
          <div className="mt-[3cqw] grid flex-1 auto-rows-min grid-cols-[repeat(auto-fit,minmax(0,1fr))] gap-[2cqw]">
            {slide.stats.map((s, i) => (
              <div key={i} className="flex flex-col rounded-[1cqw] bg-slide-panel p-[2.4cqw]">
                <span className="text-[5.2cqw] leading-none font-extrabold tracking-tight text-slide-accent tabular-nums">
                  {s.value}
                </span>
                <span className="mt-[1.4cqw] text-[1.8cqw] leading-snug">{s.label}</span>
              </div>
            ))}
          </div>
          <Body text={slide.body} />
        </>
      );
    case "quote":
      return (
        <div className="flex h-full flex-col justify-center">
          <span className="text-[2cqw] font-semibold text-slide-muted">{slide.heading}</span>
          <blockquote className="mt-[2cqw] border-l-[0.8cqw] border-slide-warm pl-[3cqw]">
            <p className="text-[3.4cqw] leading-snug font-semibold break-keep">“{slide.quote?.text}”</p>
            {slide.quote?.attribution && (
              <footer className="mt-[2cqw] text-[1.9cqw] text-slide-muted">— {slide.quote.attribution}</footer>
            )}
          </blockquote>
        </div>
      );
    case "closing":
      return (
        <>
          <Heading className="text-slide-accent">{slide.heading}</Heading>
          {slide.subheading && <p className="mt-[1cqw] text-[2.1cqw] text-slide-muted">{slide.subheading}</p>}
          <Body text={slide.body} />
          <Bullets items={slide.bullets} check />
        </>
      );
    default:
      return (
        <>
          <Heading>{slide.heading}</Heading>
          {slide.subheading && <p className="mt-[1cqw] text-[2.1cqw] text-slide-warm">{slide.subheading}</p>}
          <Body text={slide.body} />
          <Bullets items={slide.bullets} />
        </>
      );
  }
}
