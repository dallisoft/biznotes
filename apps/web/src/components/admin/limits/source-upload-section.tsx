"use client";

import { useState } from "react";
import { LimitSaveActions, LimitSection } from "@/components/admin/limits/limit-section";
import { FormField } from "@/components/layout/form-section";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status-badge";
import { useSaveSourceUploadSettings, useSourceUploadSettings } from "@/hooks/use-admin-core-limits";
import type { SourceUploadSettings } from "@/lib/admin-types";
import {
  bytesToMegabytes,
  formatUploadSize,
  limitValidationMessages,
  megabytesToBytes,
  MIB,
} from "@/lib/core-limits";
import { formatNumber } from "@/lib/format";

/** 업로드 최대 크기(코어 #388) — 파일 1개의 상한. 재정의를 지우면 env `SOURCE_MAX_UPLOAD_BYTES`로 돌아간다. */
export function SourceUploadSection() {
  const query = useSourceUploadSettings();
  return (
    <LimitSection
      title="업로드 최대 크기"
      description="소스 파일 1개의 최대 크기입니다. 재정의가 없으면 env(SOURCE_MAX_UPLOAD_BYTES) 값을 쓰고, 다음 업로드부터 적용됩니다."
      query={query}
    >
      {(data) => <SourceUploadForm data={data} />}
    </LimitSection>
  );
}

const FIELD_INFO = { max_upload_bytes: { label: "업로드 최대 크기", format: formatUploadSize } };
/** 재정의 하한 — 코어 범위는 1바이트부터다(D4 「코어 범위를 그대로 받는다」). MB 입력에서는 1/1,048,576MB다 */
const MIN_OVERRIDE_BYTES = 1;

function SourceUploadForm({ data }: { data: SourceUploadSettings }) {
  const save = useSaveSourceUploadSettings();
  const saved = data.override === null ? "" : String(bytesToMegabytes(data.override));
  // 입력은 MB(1 MB = 1,048,576바이트), 빈칸은 재정의 없음(env 값). 편집하지 않았으면(null) 서버 값을 그대로 따른다.
  const [edit, setEdit] = useState<string | null>(null);
  const value = edit ?? saved;
  const dirty = value !== saved;
  const hardMaxMb = bytesToMegabytes(data.hard_max_upload_bytes);
  // 보낼 바이트 — MB를 반올림하고 코어 범위(1바이트~하드 상한)로 맞춘다
  const toBytes = (megabytes: string) =>
    megabytesToBytes(Number(megabytes), { min: MIN_OVERRIDE_BYTES, max: data.hard_max_upload_bytes });
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
          { max_upload_bytes: value === "" ? null : toBytes(value) },
          // 보낸 값 그대로일 때만 편집을 비운다 — 저장 중에 다시 고친 값은 남긴다
          { onSuccess: () => setEdit((current) => (current === submitted ? null : current)) },
        );
      }}
      className="flex flex-col gap-4"
    >
      <dl className="grid gap-x-6 gap-y-1 text-sm sm:grid-cols-[auto_1fr]">
        <dt className="text-muted-foreground">env 기본값</dt>
        <dd className="tabular-nums" data-testid="upload-env-value">
          {formatUploadSize(data.env_max_upload_bytes)}
        </dd>
        <dt className="text-muted-foreground">적용 값</dt>
        <dd className="flex items-center gap-2" data-testid="upload-effective-value">
          <span className="tabular-nums">{formatUploadSize(data.max_upload_bytes)}</span>
          <StatusBadge tone={data.override === null ? "neutral" : "info"}>
            {data.override === null ? "env" : "재정의"}
          </StatusBadge>
        </dd>
      </dl>
      <FormField
        label="업로드 최대 크기 재정의"
        unit="MB"
        range={`1바이트~${formatNumber(hardMaxMb)}MB`}
        help="빈칸은 재정의 없음(env 값)입니다. 1MB = 1,048,576바이트이고, 1MB보다 작은 값은 소수로 넣습니다(예: 0.5)."
      >
        {(field) => (
          <div className="flex items-center gap-2">
            <Input
              {...field}
              type="number"
              min={MIN_OVERRIDE_BYTES / MIB}
              max={hardMaxMb}
              step="any"
              placeholder="없음"
              value={value}
              onChange={(e) => change(e.target.value)}
            />
            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-label="업로드 최대 크기 재정의 지우기"
              disabled={value === ""}
              onClick={() => change("")}
              className="coarse:min-h-11"
            >
              지우기
            </Button>
          </div>
        )}
      </FormField>
      {dirty && value !== "" && Number(value) >= MIN_OVERRIDE_BYTES / MIB && Number(value) <= hardMaxMb && (
        <p data-testid="upload-bytes-preview" className="text-xs text-muted-foreground">
          저장하면 {formatNumber(toBytes(value))}바이트
        </p>
      )}
      {/* compose 기본 구성은 api 앞에 프록시가 없다. 운영자가 둔 리버스 프록시의 본문 상한은 앱이 알 수 없으므로
          값과 상관없이 늘 안내한다(nginx 기본 1MB) */}
      <p data-testid="upload-proxy-hint" className="text-xs text-muted-foreground">
        api 앞에 리버스 프록시를 두었다면 그 요청 본문 상한(nginx <code>client_max_body_size</code> 등)을 적용 값보다
        크게 둡니다(멀티파트 여유 포함) — 작으면 큰 파일이 서버에 닿기 전에 프록시에서 413으로 막힙니다.
      </p>
      <LimitSaveActions
        dirty={dirty}
        saving={save.isPending}
        saveLabel="업로드 크기 저장"
        onReset={() => {
          save.reset();
          setEdit(null);
        }}
        errors={errors}
      />
    </form>
  );
}
