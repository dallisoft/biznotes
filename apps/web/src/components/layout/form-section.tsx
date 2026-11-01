"use client";

import { useId } from "react";
import { cn } from "@/lib/utils";

/**
 * 설정 폼 섹션(§6.5): ≥ 1024 왼쪽 1/3 제목·설명, 오른쪽 2/3 필드. 미만은 세로.
 * 제목은 기본 `h2`다. 페이지 섹션(`h2`) 안에 여러 개를 묶을 때는 `headingLevel={3}`으로 한 단계 내린다
 * (예: RAG 관리 「검색 설정」 아래 4개 — 제목 계층이 같은 수준으로 납작해지지 않게, §6.8).
 */
export function FormSection({
  title,
  description,
  headingLevel = 2,
  children,
  className,
}: {
  title: string;
  description?: React.ReactNode;
  headingLevel?: 2 | 3;
  children: React.ReactNode;
  className?: string;
}) {
  const titleId = useId();
  const Heading = headingLevel === 3 ? "h3" : "h2";
  return (
    <section
      data-slot="form-section"
      aria-labelledby={titleId}
      className={cn("grid gap-4 py-6 first:pt-0 lg:grid-cols-3 lg:gap-8", className)}
    >
      <div>
        <Heading id={titleId} className="text-base font-semibold">
          {title}
        </Heading>
        {description && <p className="mt-1 text-sm text-muted-foreground">{description}</p>}
      </div>
      <div className="flex flex-col gap-4 lg:col-span-2">{children}</div>
    </section>
  );
}

/** FormField가 입력 요소에 넘기는 속성 — `<Input {...field} />`처럼 펼친다. */
export interface FormFieldControlProps {
  id: string;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
}

/**
 * 폼 필드(§6.5 FormSection 규칙): 라벨 · 단위(입력 오른쪽 접미사) · 범위 · 도움말(`aria-describedby`)
 * · 검증 오류(`aria-invalid` + 문구). 입력 요소는 render prop으로 받아 id·설명 연결을 넘긴다.
 */
export function FormField({
  label,
  unit,
  range,
  help,
  error,
  children,
}: {
  label: string;
  /** 입력 오른쪽 접미사 — 예: 「토큰」 */
  unit?: string;
  /** 허용 범위 — 예: 「0~1」 */
  range?: string;
  help?: React.ReactNode;
  error?: string;
  children: (control: FormFieldControlProps) => React.ReactNode;
}) {
  const id = useId();
  const helpId = `${id}-help`;
  const errorId = `${id}-error`;
  const describedBy = [help || range ? helpId : null, error ? errorId : null]
    .filter(Boolean)
    .join(" ");
  const control: FormFieldControlProps = {
    id,
    ...(describedBy ? { "aria-describedby": describedBy } : {}),
    ...(error ? { "aria-invalid": true as const } : {}),
  };
  return (
    <div data-slot="form-field" className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      <div className="flex items-center gap-2">
        <div className="min-w-0 flex-1">{children(control)}</div>
        {unit && <span className="shrink-0 text-sm text-muted-foreground">{unit}</span>}
      </div>
      {(help || range) && (
        <p id={helpId} className="text-xs text-muted-foreground">
          {help}
          {help && range ? " " : null}
          {range && `범위 ${range}`}
        </p>
      )}
      {error && (
        <p id={errorId} className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
