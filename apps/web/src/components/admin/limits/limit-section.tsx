"use client";

import type { UseQueryResult } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { useId } from "react";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { LoadingState } from "@/components/ui/loading-state";
import { objectParticle } from "@/lib/notify";

/**
 * 「사용 제한」 코어 항목(코어 v0.26.0 #386·#387·#388) 섹션의 틀 — 제목(h2)·설명·불러오기 상태.
 * 섹션마다 따로 저장한다(위 「요청당 한도」의 저장 막대와 묶지 않는다).
 */
export function LimitSection<T>({
  title,
  description,
  query,
  children,
}: {
  title: string;
  description: string;
  query: Pick<UseQueryResult<T>, "data" | "isError" | "isFetching" | "refetch">;
  children: (data: T) => React.ReactNode;
}) {
  const titleId = useId();
  const target = `${title}${objectParticle(title)}`;
  return (
    <section aria-labelledby={titleId} className="mt-10">
      <h2 id={titleId} className="text-lg font-semibold">
        {title}
      </h2>
      <p className="mt-1 text-sm text-muted-foreground">{description}</p>
      <div className="mt-6">
        {query.data === undefined ? (
          query.isError ? (
            <ErrorState
              title={`${target} 불러오지 못했습니다`}
              onRetry={() => void query.refetch()}
              retrying={query.isFetching}
            />
          ) : (
            <LoadingState variant="form" count={1} label={`${target} 불러오는 중`} />
          )
        ) : (
          children(query.data)
        )}
      </div>
    </section>
  );
}

/** 섹션 안 안내 상자 — 동작이 바뀌는 사실(기본값 적용 대상·프록시 상한)을 알린다. */
export function LimitNotice({ children, testId }: { children: React.ReactNode; testId?: string }) {
  return (
    <div
      data-testid={testId}
      className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning-text"
    >
      <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
      <div>{children}</div>
    </div>
  );
}

/**
 * 섹션 저장 줄 — 「{saveLabel}」(바뀐 값이 있을 때만 누를 수 있다) + 「변경 취소」 + 저장 검증 오류(422) 문구.
 * 버튼 이름은 섹션마다 다르다(한 화면에 저장 단위가 넷이다).
 */
export function LimitSaveActions({
  dirty,
  saving,
  saveLabel,
  onReset,
  errors,
}: {
  dirty: boolean;
  saving: boolean;
  saveLabel: string;
  onReset: () => void;
  errors: string[] | null;
}) {
  return (
    <div className="mt-4 flex flex-col gap-3">
      {errors && errors.length > 0 && (
        <div role="alert" className="text-sm text-destructive">
          <p className="font-medium">저장하지 못했습니다</p>
          <ul className="mt-1 list-disc pl-5">
            {errors.map((message, i) => (
              <li key={`${i}-${message}`}>{message}</li>
            ))}
          </ul>
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={!dirty || saving} className="coarse:min-h-11">
          {saving ? "저장 중…" : saveLabel}
        </Button>
        {dirty && (
          <Button type="button" variant="outline" onClick={onReset} disabled={saving} className="coarse:min-h-11">
            변경 취소
          </Button>
        )}
      </div>
    </div>
  );
}
