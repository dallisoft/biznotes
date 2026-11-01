import { cn } from "@/lib/utils";

export type StatusTone = "neutral" | "info" | "success" | "warning" | "danger";

// 틴트 배경 + `--{tone}-text` 글자(plan §6.4.1 — 라이트 10%, 다크 15% 틴트에서 4.5:1 이상, U9).
// danger 톤은 `destructive` 계열 토큰을 쓴다(`--destructive-text`가 danger 톤의 -text와 같은 값).
const TONE_CLASS: Record<StatusTone, string> = {
  neutral: "bg-muted text-muted-foreground",
  info: "bg-info/10 text-info-text dark:bg-info/15",
  success: "bg-success/10 text-success-text dark:bg-success/15",
  warning: "bg-warning/10 text-warning-text dark:bg-warning/15",
  danger: "bg-destructive/10 text-destructive-text dark:bg-destructive/15",
};

export interface StatusBadgeProps extends Omit<React.ComponentProps<"span">, "children"> {
  tone?: StatusTone;
  /** 상태 텍스트 — 필수(DP-4: 색·아이콘만으로 상태를 전하지 않는다). 문자열·숫자만 받는다. */
  children: string | number;
  /** 텍스트 앞 아이콘(선택, 장식 — 스크린리더에서 숨긴다). */
  icon?: React.ReactNode;
  /** 진행 중 상태 깜박임(선택) — 움직임 줄이기 설정이면 멈춘다. */
  pulse?: boolean;
}

/** 상태 배지(§6.5): 의미 톤 + 텍스트 필수. */
export function StatusBadge({
  tone = "neutral",
  children,
  icon,
  pulse = false,
  className,
  ...props
}: StatusBadgeProps) {
  return (
    <span
      data-slot="status-badge"
      data-tone={tone}
      className={cn(
        "inline-flex h-5 w-fit shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap [&>svg]:size-3 [&>svg]:shrink-0",
        TONE_CLASS[tone],
        pulse && "motion-safe:animate-pulse",
        className,
      )}
      {...props}
    >
      {icon && (
        <span aria-hidden className="inline-flex">
          {icon}
        </span>
      )}
      {children}
    </span>
  );
}
