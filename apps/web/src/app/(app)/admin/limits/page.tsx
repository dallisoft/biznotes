"use client";

import { useEffect, useId, useRef, useState } from "react";
import { CoreLimitSections } from "@/components/admin/limits/core-limit-sections";
import { StickySaveBar } from "@/components/layout/sticky-save-bar";
import { PageHeader } from "@/components/shell/page-header";
import { ErrorState } from "@/components/ui/error-state";
import { LoadingState } from "@/components/ui/loading-state";
import { useLimitSettings, useSaveLimitSettings } from "@/hooks/use-admin-limits";
import type { LimitSettings } from "@/lib/admin-types";
import { changedLimits, RequestLimitFields } from "./request-limit-fields";

/** 두 설정이 같은가 — 키 합집합을 본다(필드가 늘어도 그대로 쓴다). */
function sameLimits(a: LimitSettings, b: LimitSettings): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]) as Set<keyof LimitSettings>;
  return [...keys].every((k) => a[k] === b[k]);
}

/**
 * 재조회한 서버 값으로 폼을 맞춘다 — 편집한 필드(직전 기준과 다른 값)만 남기고 나머지는 새 값이다.
 * 폼 전체를 남기면 그사이 다른 관리자가 바꾼 항목이 「바꾼 항목」으로 PUT에 실려 되돌려진다.
 */
function syncUnedited(form: LimitSettings, previous: LimitSettings, data: LimitSettings): LimitSettings {
  const next = { ...data };
  for (const k of Object.keys(form) as Array<keyof LimitSettings>) {
    if (form[k] !== previous[k]) next[k] = form[k];
  }
  return next;
}

export default function AdminLimitsPage() {
  const settings = useLimitSettings();
  const save = useSaveLimitSettings();
  // 폼과 기준 설정(서버에서 받은 값 · 방금 저장한 값). 둘이 다를 때만 저장 막대가 보인다.
  const [form, setForm] = useState<LimitSettings | null>(null);
  const [baseline, setBaselineState] = useState<LimitSettings | null>(null);
  // 서버 값이 바뀔 때 직전 기준과 비교한다 — 효과 안에서 최신 기준을 읽으려고 ref로도 둔다
  const baselineRef = useRef<LimitSettings | null>(null);
  const setBaseline = (next: LimitSettings) => {
    baselineRef.current = next;
    setBaselineState(next);
  };
  useEffect(() => {
    if (!settings.data) return;
    const data = settings.data;
    const previous = baselineRef.current;
    baselineRef.current = data;
    setBaselineState(data);
    // 저장 중·저장 직후의 재조회가 그 사이 편집을 덮지 않게 — 편집하지 않은(기준과 같은) 필드만
    // 서버 값으로 맞춘다
    setForm((current) =>
      current === null || previous === null ? data : syncUnedited(current, previous, data),
    );
  }, [settings.data]);
  const dirty = form !== null && baseline !== null && !sameLimits(form, baseline);
  const formRef = useRef<HTMLFormElement>(null);
  const titleId = useId();

  return (
    <>
      <PageHeader
        breadcrumb={[{ label: "관리", href: "/admin" }, { label: "사용 제한" }]}
        title="사용 제한"
        description="요청 1건이 쓸 수 있는 자원과 시스템 전체 사용의 상한을 정합니다"
      />
      <section aria-labelledby={titleId}>
        <h2 id={titleId} className="text-lg font-semibold">
          요청당 한도
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          사용자별 일일 한도는 「사용자·그룹」에서 정합니다. 바꾼 값은 「변경 사항 저장」을 눌러야 적용됩니다.
        </p>
        <div className="mt-6">
          {form === null ? (
            settings.isError ? (
              <ErrorState
                title="사용 제한을 불러오지 못했습니다"
                onRetry={() => void settings.refetch()}
                retrying={settings.isFetching}
              />
            ) : (
              <LoadingState variant="form" count={1} label="사용 제한을 불러오는 중" />
            )
          ) : (
            <form
              ref={formRef}
              aria-labelledby={titleId}
              onSubmit={(e) => {
                e.preventDefault();
                if (!baseline) return;
                // 바꾼 항목만 보낸다. 저장되면 제출한 폼 전체가 새 기준이다
                const submitted = form;
                save.mutate(changedLimits(form, baseline), { onSuccess: () => setBaseline(submitted) });
              }}
              className="divide-y"
            >
              <RequestLimitFields form={form} onChange={setForm} />
            </form>
          )}
        </div>
        {/* 폼 밖에 둔다 — 「되돌리기」가 폼을 제출하지 않게. 저장은 requestSubmit으로 입력 범위 검증을 거친다. */}
        <StickySaveBar
          dirty={dirty}
          saving={save.isPending}
          onSave={() => formRef.current?.requestSubmit()}
          onReset={() => {
            if (baseline) setForm(baseline);
          }}
        />
      </section>
      <CoreLimitSections />
    </>
  );
}
