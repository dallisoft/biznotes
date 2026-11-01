"use client";

import { useState } from "react";
import { LimitNotice, LimitSaveActions, LimitSection } from "@/components/admin/limits/limit-section";
import { FormField } from "@/components/layout/form-section";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useSaveUsageDefaults, useUsageDefaults } from "@/hooks/use-admin-core-limits";
import type { UsageDefaults, UsageLimitField } from "@/lib/admin-types";
import { limitValidationMessages, settleEdits, USAGE_LIMIT_FIELDS, USAGE_LIMIT_MAX } from "@/lib/core-limits";
import { formatNumber } from "@/lib/format";

/** 일일 한도 기본값(코어 #387) — 사용자별 값이 없는 사용자에게 적용된다. 지우면 기본값 없음(무제한). */
export function UsageDefaultsSection() {
  const query = useUsageDefaults();
  return (
    <LimitSection
      title="일일 한도 기본값"
      description="「사용자·그룹」에서 한도를 정하지 않은 사용자에게 적용하는 일일 한도입니다. 빈칸은 기본값 없음(무제한)입니다."
      query={query}
    >
      {(data) => <UsageDefaultsForm data={data} />}
    </LimitSection>
  );
}

const savedText = (data: UsageDefaults, key: UsageLimitField) => {
  const value = data[key];
  return value === null || value === undefined ? "" : String(value);
};

const FIELD_INFO = Object.fromEntries(USAGE_LIMIT_FIELDS.map((f) => [f.key, { label: f.label }]));

function UsageDefaultsForm({ data }: { data: UsageDefaults }) {
  const save = useSaveUsageDefaults();
  // 바꾼 칸만 담는다(빈 문자열 = 기본값 지움). 편집하지 않은 칸은 서버 값(재조회·저장 응답)을 그대로 따른다.
  const [edits, setEdits] = useState<Partial<Record<UsageLimitField, string>>>({});
  const valueOf = (key: UsageLimitField) => edits[key] ?? savedText(data, key);
  // 부분 갱신 — 바꾼 필드만 보낸다. 빈칸은 `null`(기본값 삭제)
  const changes: UsageDefaults = {};
  for (const { key } of USAGE_LIMIT_FIELDS) {
    const value = valueOf(key);
    if (value !== savedText(data, key)) changes[key] = value === "" ? null : Number(value);
  }
  const dirty = Object.keys(changes).length > 0;
  const errors = limitValidationMessages(save.error, FIELD_INFO);
  const edit = (key: UsageLimitField, value: string) => {
    if (save.isError) save.reset();
    setEdits((prev) => ({ ...prev, [key]: value }));
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (!dirty) return;
        // 보낸 칸만 비운다 — 저장 중에 다시 고친 값은 남긴다
        const submitted = edits;
        save.mutate(changes, { onSuccess: () => setEdits((current) => settleEdits(current, submitted)) });
      }}
      className="flex flex-col gap-4"
    >
      <LimitNotice testId="usage-defaults-notice">
        기본값을 두면 사용자별 한도가 없는 사용자도 이 한도를 받습니다 — 지금까지 이 사용자들은 무제한이었습니다.
        사용자별 값이 있는 항목은 그 값이 우선하고, 관리자 역할은 한도 검사에서 빠집니다. 기본값을 둔 뒤 특정 사용자를
        풀어 주려면 「사용자·그룹」에서 그 사용자에게 큰 값을 넣으세요.
      </LimitNotice>
      <div className="grid gap-4 sm:grid-cols-2">
        {USAGE_LIMIT_FIELDS.map((f) => {
          const value = valueOf(f.key);
          return (
            <FormField
              key={f.key}
              label={f.label}
              range={`0~${formatNumber(USAGE_LIMIT_MAX)}`}
              help="빈칸은 기본값 없음(무제한), 0은 사용을 막습니다."
            >
              {(field) => (
                <div className="flex items-center gap-2">
                  <Input
                    {...field}
                    type="number"
                    min={0}
                    max={USAGE_LIMIT_MAX}
                    step={1}
                    placeholder="무제한"
                    value={value}
                    onChange={(e) => edit(f.key, e.target.value)}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label={`${f.label} 기본값 지우기`}
                    disabled={value === ""}
                    onClick={() => edit(f.key, "")}
                    className="coarse:min-h-11"
                  >
                    지우기
                  </Button>
                </div>
              )}
            </FormField>
          );
        })}
      </div>
      <LimitSaveActions
        dirty={dirty}
        saving={save.isPending}
        saveLabel="기본값 저장"
        onReset={() => {
          save.reset();
          setEdits({});
        }}
        errors={errors}
      />
    </form>
  );
}
