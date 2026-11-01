import { Skeleton } from "@/components/ui/skeleton";

export type LoadingStateVariant = "cards" | "table" | "form";

const DEFAULT_COUNT: Record<LoadingStateVariant, number> = { cards: 6, table: 5, form: 3 };

/**
 * 로딩 상태 프리셋(§6.5) — 기존 `Skeleton`으로 카드 그리드·표 행·폼 섹션 모양을 그린다.
 * 스크린리더에는 `role="status"` 「불러오는 중」만 읽히고 스켈레톤은 숨긴다.
 */
export function LoadingState({
  variant = "cards",
  count,
  label = "불러오는 중",
  className,
}: {
  variant?: LoadingStateVariant;
  count?: number;
  label?: string;
  className?: string;
}) {
  const n = count ?? DEFAULT_COUNT[variant];
  const items = Array.from({ length: n }, (_, i) => i);
  return (
    <div role="status" data-slot="loading-state" data-variant={variant} className={className}>
      <span className="sr-only">{label}</span>
      <div aria-hidden>
        {variant === "cards" && (
          // 열 수는 콘텐츠 폭이 정한다(T1, plan §6.6.1) — 노트북·스킬·워크플로우 카드 그리드와 같은 260px 기준(P3 IR-H7)
          <div className="grid grid-cols-[repeat(auto-fill,minmax(min(260px,100%),1fr))] gap-4">
            {items.map((i) => (
              <Skeleton key={i} data-testid="loading-skeleton" className="h-32 rounded-xl" />
            ))}
          </div>
        )}
        {variant === "table" && (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-8 w-full" />
            {items.map((i) => (
              <Skeleton key={i} data-testid="loading-skeleton" className="h-10 w-full" />
            ))}
          </div>
        )}
        {variant === "form" && (
          <div className="flex flex-col gap-8">
            {items.map((i) => (
              <div key={i} data-testid="loading-skeleton" className="grid gap-4 lg:grid-cols-3">
                <div className="flex flex-col gap-2">
                  <Skeleton className="h-5 w-32" />
                  <Skeleton className="h-4 w-48" />
                </div>
                <div className="flex flex-col gap-3 lg:col-span-2">
                  <Skeleton className="h-8 w-full" />
                  <Skeleton className="h-8 w-2/3" />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
