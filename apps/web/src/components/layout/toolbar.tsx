import { Search } from "lucide-react";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

/**
 * 목록 화면 툴바(§6.5): 왼쪽 = 검색·필터(children), 오른쪽 = 보기 전환 등(`end`). 640 미만에서는 줄바꿈한다.
 * 키보드 화살표 이동을 관리하지 않으므로 `role="toolbar"`를 쓰지 않는다.
 */
export function Toolbar({
  children,
  end,
  className,
  ...props
}: React.ComponentProps<"div"> & { end?: React.ReactNode }) {
  return (
    <div
      data-slot="toolbar"
      className={cn("mb-4 flex flex-wrap items-center gap-2", className)}
      {...props}
    >
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">{children}</div>
      {end && <div className="ml-auto flex shrink-0 items-center gap-2">{end}</div>}
    </div>
  );
}

/** 툴바 검색 입력 — 돋보기 아이콘 + 접근 가능한 이름(`label`, 필수). */
export function ToolbarSearch({
  label,
  className,
  placeholder,
  ...props
}: Omit<React.ComponentProps<"input">, "aria-label" | "type"> & { label: string }) {
  return (
    <div className={cn("relative w-full sm:w-64", className)}>
      <Search
        aria-hidden
        className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground"
      />
      <Input type="search" aria-label={label} placeholder={placeholder ?? label} className="pl-8 coarse:min-h-11" {...props} />
    </div>
  );
}
