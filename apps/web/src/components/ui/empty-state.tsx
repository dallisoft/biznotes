import { SearchX } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface EmptyStateAction {
  label: string;
  onClick: () => void;
  icon?: React.ReactNode;
}

/**
 * 빈 상태(§6.5): 아이콘(48px 타일) + 제목 + 설명 1줄 + 주 동작 버튼(선택).
 * 예: 「아직 노트북이 없습니다」 + 「새 노트북」.
 */
export function EmptyState({
  icon,
  title,
  description,
  action,
  actionVariant = "default",
  className,
}: {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: EmptyStateAction;
  /** 필터 지우기처럼 보조 동작이면 outline */
  actionVariant?: "default" | "outline";
  className?: string;
}) {
  return (
    <div
      data-slot="empty-state"
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-xl border border-dashed px-6 py-12 text-center",
        className,
      )}
    >
      {icon && (
        <div
          aria-hidden
          className="flex size-12 items-center justify-center rounded-xl bg-muted text-muted-foreground [&_svg]:size-6"
        >
          {icon}
        </div>
      )}
      <div className="flex flex-col gap-1">
        <p className="text-base font-semibold">{title}</p>
        {description && <p className="text-sm text-muted-foreground">{description}</p>}
      </div>
      {action && (
        <Button variant={actionVariant} onClick={action.onClick} className="coarse:h-11 coarse:min-w-11">
          {action.icon}
          {action.label}
        </Button>
      )}
    </div>
  );
}

/** 필터 결과 없음(§6.5): 「조건에 맞는 항목이 없습니다」 + 「필터 지우기」. */
export function FilteredEmptyState({
  onClearFilters,
  className,
}: {
  onClearFilters: () => void;
  className?: string;
}) {
  return (
    <EmptyState
      icon={<SearchX />}
      title="조건에 맞는 항목이 없습니다"
      description="검색어나 필터를 바꿔 보세요."
      action={{ label: "필터 지우기", onClick: onClearFilters }}
      actionVariant="outline"
      className={className}
    />
  );
}
