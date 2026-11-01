"use client";

import { useState } from "react";
import { AdminTableFrame } from "@/components/admin/admin-table-frame";
import { LimitNotice, LimitSaveActions, LimitSection } from "@/components/admin/limits/limit-section";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useRateLimits, useSaveRateLimits } from "@/hooks/use-admin-core-limits";
import type { RateLimitItem, RateLimitKey, RateLimits, RateLimitsUpdate } from "@/lib/admin-types";
import { limitValidationMessages, RATE_LIMIT_LABEL, rateLimitLabel, settleEdits } from "@/lib/core-limits";
import { formatNumber } from "@/lib/format";

/** 분당 레이트 리밋(코어 #386) — 키별 env 기본값·재정의·적용값. 재정의를 지우면 env 값으로 돌아간다. */
export function RateLimitsSection() {
  const query = useRateLimits();
  return (
    <LimitSection
      title="분당 레이트 리밋"
      description="사용자 1명이 1분에 보낼 수 있는 요청 수입니다. 재정의가 없으면 env(배포 설정) 값을 씁니다."
      query={query}
    >
      {(data) => <RateLimitsForm data={data} />}
    </LimitSection>
  );
}

/** 분당 횟수 — 0 이하는 env에서 그 리밋을 끈 상태다. */
function perMinute(value: number): string {
  return value <= 0 ? "꺼짐" : formatNumber(value);
}

const savedText = (item: RateLimitItem) => (item.override === null ? "" : String(item.override));

function RateLimitsForm({ data }: { data: RateLimits }) {
  const save = useSaveRateLimits();
  // 바꾼 칸만 담는다(빈 문자열 = 재정의 지움). 편집하지 않은 칸은 서버 값(재조회·저장 응답)을 그대로 따른다.
  const [edits, setEdits] = useState<Partial<Record<RateLimitKey, string>>>({});
  const items = data.items ?? [];
  const valueOf = (item: RateLimitItem) => edits[item.key] ?? savedText(item);
  // 부분 갱신 — 바꾼 키만 보낸다. 빈칸은 `null`(재정의 삭제)
  const changes: RateLimitsUpdate = {};
  for (const item of items) {
    const value = valueOf(item);
    if (value !== savedText(item)) changes[item.key] = value === "" ? null : Number(value);
  }
  const dirty = Object.keys(changes).length > 0;
  const errors = limitValidationMessages(
    save.error,
    Object.fromEntries(items.map((item) => [item.key, { label: rateLimitLabel(item.key) }])),
  );
  const edit = (key: RateLimitKey, value: string) => {
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
      <p className="text-sm text-muted-foreground">
        저장한 값은 이 API 프로세스에는 곧바로, 다른 API 프로세스에는 최대 {formatNumber(data.cache_ttl_seconds)}초
        뒤에 반영됩니다(재기동 없음). 0 이하(리밋 끄기)는 env에서만 정합니다.
      </p>
      {data.enabled ? (
        <p data-testid="rate-limit-enabled" className="flex items-center gap-2 text-sm">
          <StatusBadge tone="success">켜짐</StatusBadge>
          <span className="text-muted-foreground">
            레이트 리밋 전체(<code>RATE_LIMIT_ENABLED</code>) — env에서만 켜고 끕니다
          </span>
        </p>
      ) : (
        <LimitNotice testId="rate-limit-enabled">
          레이트 리밋이 전체 꺼져 있습니다(<code>RATE_LIMIT_ENABLED=false</code>). 아래 값을 저장해도 적용되지 않습니다 —
          켜고 끄기는 env에서만 합니다.
        </LimitNotice>
      )}
      <AdminTableFrame>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>항목</TableHead>
              <TableHead className="text-right">env 기본값 (회/분)</TableHead>
              <TableHead>재정의 (회/분)</TableHead>
              <TableHead className="text-right">적용 값 (회/분)</TableHead>
              <TableHead>
                <span className="sr-only">동작</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((item) => {
              const label = rateLimitLabel(item.key);
              const help = Object.prototype.hasOwnProperty.call(RATE_LIMIT_LABEL, item.key)
                ? RATE_LIMIT_LABEL[item.key].help
                : undefined;
              const value = valueOf(item);
              const clearing = value === "" && item.override !== null;
              return (
                <TableRow key={item.key} data-testid={`rate-limit-${item.key}`}>
                  <TableCell>
                    <div className="font-medium">{label}</div>
                    {help && <div className="text-xs whitespace-normal text-muted-foreground">{help}</div>}
                  </TableCell>
                  <TableCell className="text-right tabular-nums" data-testid="env-value">
                    {perMinute(item.env_value)}
                  </TableCell>
                  <TableCell>
                    <Input
                      type="number"
                      aria-label={`${label} 재정의`}
                      min={data.min_value}
                      max={data.max_value}
                      step={1}
                      placeholder="없음"
                      value={value}
                      onChange={(e) => edit(item.key, e.target.value)}
                      className="w-28"
                    />
                    {clearing && (
                      <p className="mt-1 text-xs text-muted-foreground">저장하면 env 값으로 돌아갑니다</p>
                    )}
                  </TableCell>
                  <TableCell className="text-right" data-testid="effective-value">
                    <span className="inline-flex items-center gap-2">
                      <span className="tabular-nums">{perMinute(item.effective)}</span>
                      <StatusBadge tone={item.override === null ? "neutral" : "info"}>
                        {item.override === null ? "env" : "재정의"}
                      </StatusBadge>
                    </span>
                  </TableCell>
                  <TableCell>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      aria-label={`${label} 재정의 지우기`}
                      disabled={value === ""}
                      onClick={() => edit(item.key, "")}
                      className="coarse:min-h-11"
                    >
                      지우기
                    </Button>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </AdminTableFrame>
      <p className="text-xs text-muted-foreground">
        재정의 범위 {formatNumber(data.min_value)}~{formatNumber(data.max_value)}회/분. 빈칸은 재정의 없음(env 값)입니다.
      </p>
      <LimitSaveActions
        dirty={dirty}
        saving={save.isPending}
        saveLabel="레이트 리밋 저장"
        onReset={() => {
          save.reset();
          setEdits({});
        }}
        errors={errors}
      />
    </form>
  );
}
