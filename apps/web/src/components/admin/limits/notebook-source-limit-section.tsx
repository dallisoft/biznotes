"use client";

import { useState } from "react";
import { LimitNotice, LimitSaveActions, LimitSection } from "@/components/admin/limits/limit-section";
import { FormField } from "@/components/layout/form-section";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status-badge";
import { useNotebookSourceLimit, useSaveNotebookSourceLimit } from "@/hooks/use-admin-core-limits";
import type { NotebookSourceLimit } from "@/lib/admin-types";
import {
  limitValidationMessages,
  NOTEBOOK_SOURCE_LIMIT_MAX,
  RECOMMENDED_MAX_SOURCES,
} from "@/lib/core-limits";
import { formatNumber } from "@/lib/format";

/**
 * 노트북당 소스 상한(코어 v0.30.0 #445, #281). 재정의를 지우면 env `MAX_SOURCES_PER_NOTEBOOK`(배포값 500)로
 * 돌아간다. 낮춰도 기존 소스는 그대로 두고 추가만 막는다. 500을 넘기면 경고만 보인다(D4 — 막지 않는다).
 */
export function NotebookSourceLimitSection() {
  const query = useNotebookSourceLimit();
  return (
    <LimitSection
      title="노트북당 소스 수"
      description="노트북 하나에 추가할 수 있는 소스 수입니다. 재정의가 없으면 env(MAX_SOURCES_PER_NOTEBOOK) 값을 쓰고, 다음 소스 추가부터 적용됩니다. 낮춰도 이미 있는 소스는 지우지 않습니다."
      query={query}
    >
      {(data) => <NotebookSourceLimitForm data={data} />}
    </LimitSection>
  );
}

const FIELD_INFO = {
  max_sources_per_notebook: { label: "노트북당 소스 수", format: (v: number) => `${formatNumber(v)}개` },
};

function NotebookSourceLimitForm({ data }: { data: NotebookSourceLimit }) {
  const save = useSaveNotebookSourceLimit();
  const saved = data.override === null ? "" : String(data.override);
  // 빈칸은 재정의 없음(env 값). 편집하지 않았으면(null) 서버 값을 그대로 따른다
  const [edit, setEdit] = useState<string | null>(null);
  const value = edit ?? saved;
  const dirty = value !== saved;
  const candidate = value === "" ? data.env_max_sources_per_notebook : Number(value);
  const errors = limitValidationMessages(save.error, FIELD_INFO);
  const change = (next: string) => {
    if (save.isError) save.reset();
    setEdit(next);
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (!dirty) return;
        const submitted = value;
        save.mutate(
          { max_sources_per_notebook: value === "" ? null : Math.round(Number(value)) },
          // 보낸 값 그대로일 때만 편집을 비운다 — 저장 중에 다시 고친 값은 남긴다
          { onSuccess: () => setEdit((current) => (current === submitted ? null : current)) },
        );
      }}
      className="flex flex-col gap-4"
    >
      <dl className="grid gap-x-6 gap-y-1 text-sm sm:grid-cols-[auto_1fr]">
        <dt className="text-muted-foreground">env 기본값</dt>
        <dd className="tabular-nums" data-testid="source-limit-env-value">
          {formatNumber(data.env_max_sources_per_notebook)}개
        </dd>
        <dt className="text-muted-foreground">적용 값</dt>
        <dd className="flex items-center gap-2" data-testid="source-limit-effective-value">
          <span className="tabular-nums">{formatNumber(data.max_sources_per_notebook)}개</span>
          <StatusBadge tone={data.override === null ? "neutral" : "info"}>
            {data.override === null ? "env" : "재정의"}
          </StatusBadge>
        </dd>
      </dl>
      <FormField
        label="노트북당 소스 수 재정의"
        unit="개"
        range={`1~${formatNumber(NOTEBOOK_SOURCE_LIMIT_MAX)}`}
        help="빈칸은 재정의 없음(env 값)입니다."
      >
        {(field) => (
          <div className="flex items-center gap-2">
            <Input
              {...field}
              type="number"
              min={1}
              max={NOTEBOOK_SOURCE_LIMIT_MAX}
              step={1}
              placeholder="없음"
              value={value}
              onChange={(e) => change(e.target.value)}
            />
            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-label="노트북당 소스 수 재정의 지우기"
              disabled={value === ""}
              onClick={() => change("")}
              className="coarse:min-h-11"
            >
              지우기
            </Button>
          </div>
        )}
      </FormField>
      {Number.isFinite(candidate) && candidate > RECOMMENDED_MAX_SOURCES && (
        <LimitNotice testId="source-limit-warning">
          {formatNumber(RECOMMENDED_MAX_SOURCES)}개를 넘는 값입니다. 소스가 그보다 많은 노트북에서는 에이전트가 모든
          소스를 결과에 반영하지 못할 수 있습니다.
        </LimitNotice>
      )}
      <LimitSaveActions
        dirty={dirty}
        saving={save.isPending}
        saveLabel="소스 수 저장"
        onReset={() => {
          save.reset();
          setEdit(null);
        }}
        errors={errors}
      />
    </form>
  );
}
