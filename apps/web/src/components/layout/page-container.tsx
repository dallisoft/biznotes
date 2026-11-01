import { cn } from "@/lib/utils";

export type PageContainerWidth = "wide" | "default" | "narrow" | "bleed";

// 콘텐츠 최대 폭(plan §6.4.4): wide 1536(목록·표·대시보드) / default 1024(상세) / narrow 768(설정·안내·폼)
// / bleed 제한·패딩 없음(노트북 작업 화면·편집기).
const WIDTH_CLASS: Record<Exclude<PageContainerWidth, "bleed">, string> = {
  wide: "max-w-[96rem]",
  default: "max-w-5xl",
  narrow: "max-w-3xl",
};

/**
 * 셸 `main` 안에서 페이지의 최상위 래퍼(§6.5) — `main` 태그가 아니다(랜드마크는 셸이 하나만 둔다).
 * 반응형 패딩: < 768 16px / 768–1023 24px / ≥ 1024 32px.
 */
export function PageContainer({
  width = "wide",
  className,
  ...props
}: React.ComponentProps<"div"> & { width?: PageContainerWidth }) {
  return (
    <div
      data-slot="page-container"
      data-width={width}
      className={cn(
        width === "bleed"
          ? "h-full min-h-0 w-full"
          : cn("mx-auto w-full px-4 py-6 md:px-6 lg:px-8 lg:py-8", WIDTH_CLASS[width]),
        className,
      )}
      {...props}
    />
  );
}
