"use client";

import { Radio } from "@base-ui/react/radio";
import { RadioGroup } from "@base-ui/react/radio-group";
import { cn } from "@/lib/utils";

export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  icon?: React.ReactNode;
  /** 기존 e2e 선택자를 유지할 때(D-17) — 예: `view-mode-card`. */
  testId?: string;
}

/**
 * 단일 선택 버튼 묶음(§6.5) — 기간(7일/14일/30일)·보기 전환(카드/표). Base UI RadioGroup으로
 * `role="radiogroup"` + `role="radio"`/`aria-checked`, 화살표 키 이동(이동하면 선택)을 제공한다. 줄바꿈하지 않는다.
 */
export function SegmentedControl<T extends string>({
  label,
  options,
  value,
  onValueChange,
  iconOnlyOnNarrow = false,
  className,
}: {
  /** 묶음의 접근 가능한 이름(필수) — 예: 「기간」 「보기 방식」. */
  label: string;
  options: readonly SegmentedOption<T>[];
  value: T;
  onValueChange: (value: T) => void;
  /**
   * (선택, 기본 false) 좁은 화면(< 768)에서 아이콘이 있는 항목은 아이콘만 보인다 — 라벨은 화면 낭독기용으로 남아
   * 접근 가능한 이름은 그대로다(예: 노트북 홈 보기 전환 「카드 | 표」, 목업 M1 — P3 IR-H5).
   */
  iconOnlyOnNarrow?: boolean;
  className?: string;
}) {
  return (
    <RadioGroup
      aria-label={label}
      value={value}
      onValueChange={(next) => onValueChange(next as T)}
      className={cn(
        "inline-flex shrink-0 items-center gap-0.5 rounded-lg bg-muted p-0.5 whitespace-nowrap",
        className,
      )}
    >
      {options.map((o) => (
        <Radio.Root
          key={o.value}
          value={o.value}
          data-testid={o.testId}
          className="inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-md border border-transparent px-2.5 text-sm font-medium whitespace-nowrap text-muted-foreground transition-colors outline-none select-none hover:text-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 coarse:h-11 coarse:min-w-11 data-checked:bg-background data-checked:text-foreground data-checked:shadow-sm [&_svg]:size-4 [&_svg]:shrink-0"
        >
          {o.icon && (
            <span aria-hidden className="inline-flex">
              {o.icon}
            </span>
          )}
          {iconOnlyOnNarrow && o.icon ? <span className="max-md:sr-only">{o.label}</span> : o.label}
        </Radio.Root>
      ))}
    </RadioGroup>
  );
}
