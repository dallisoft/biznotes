/**
 * 관리 › 사용 제한의 코어 항목(코어 v0.26.0 #386·#387·#388) — 라벨·단위 변환·저장 검증 오류 문구.
 *
 * 값의 정본은 코어 `system_settings` 재정의이고 기본값은 env다. 재정의를 지우면(`null`) env 값으로 돌아간다.
 */
import { ApiError } from "@/lib/api-client";
import type { EffectiveUsageLimit, RateLimitKey, UsageLimitField } from "@/lib/admin-types";
import { formatNumber } from "@/lib/format";

/** 분당 레이트 리밋 키 → 라벨·설명. 코어 `RateLimitKey` 7종과 같다(모르는 키는 원시 키로 보인다). */
export const RATE_LIMIT_LABEL = {
  rate_limit_chat: { label: "채팅 질문", help: "채팅 메시지 전송 — LLM 비용과 바로 이어집니다" },
  rate_limit_agent_run: { label: "에이전트 실행", help: "에이전트·스튜디오 실행 — LLM 비용과 바로 이어집니다" },
  rate_limit_search: { label: "검색", help: "노트북 전체·청크 검색" },
  rate_limit_web_search: { label: "웹 검색", help: "외부 검색 프로바이더 호출" },
  rate_limit_source_create: { label: "소스 등록", help: "웹·텍스트 소스 등록" },
  rate_limit_source_upload: { label: "파일 업로드", help: "소스 파일 업로드" },
  rate_limit_source_content: { label: "소스 원문 조회", help: "파싱한 본문 전문 조회" },
} as const satisfies Record<RateLimitKey, { label: string; help: string }>;

export function rateLimitLabel(key: string): string {
  return Object.prototype.hasOwnProperty.call(RATE_LIMIT_LABEL, key)
    ? RATE_LIMIT_LABEL[key as RateLimitKey].label
    : key;
}

/** 노트북당 소스 상한 재정의 상한 — 코어 `NOTEBOOK_SOURCE_LIMIT_OVERRIDE_MAX`(#445). */
export const NOTEBOOK_SOURCE_LIMIT_MAX = 2_147_483_647;

/**
 * 결과에 모든 소스를 반영할 수 있는 노트북당 소스 수 — 배포값(#281, core-v0.30.0-upgrade D4). 관리자는 더 크게
 * 재정의할 수 있지만 화면이 경고한다(막지 않는다).
 */
export const RECOMMENDED_MAX_SOURCES = 500;

/**
 * `GET /sources/upload-limits`의 노트북당 소스 상한(코어 v0.30.0 #445). 1 이상 정수만 받고, 없거나(옛 api·조회 전)
 * 잘못된 값은 undefined다 — 호출부는 분모 없이 보이고 서버 422가 판정한다.
 */
export function sourceLimitFrom(
  limits: { max_sources_per_notebook?: number | null } | null | undefined,
): number | undefined {
  const value = limits?.max_sources_per_notebook;
  return typeof value === "number" && Number.isInteger(value) && value >= 1 ? value : undefined;
}

/** 일일 한도 필드 — 「사용자·그룹」 한도 다이얼로그와 「일일 한도 기본값」이 같이 쓴다. `todayKey`는 당일 사용량 키. */
export const USAGE_LIMIT_FIELDS = [
  { key: "daily_tokens", label: "LLM 토큰/일", todayKey: "tokens" },
  { key: "daily_agent_runs", label: "에이전트 실행/일", todayKey: "agent_runs" },
] as const satisfies ReadonlyArray<{ key: UsageLimitField; label: string; todayKey: string }>;

/** 일일 한도 상한 — 코어 `USAGE_LIMIT_MAX`(`usage_limits` 열 INTEGER 최댓값). 0은 사용을 막는다. */
export const USAGE_LIMIT_MAX = 2_147_483_647;

/** 적용 한도의 출처(코어 #387: 사용자별 값 > 시스템 기본값 > 무제한) → 라벨. */
export const USAGE_LIMIT_SOURCE_LABEL = {
  user: "사용자 지정",
  default: "기본값",
  unlimited: "무제한",
} as const satisfies Record<EffectiveUsageLimit["source"], string>;

/** 적용 한도 한 줄 — 「1,000 · 기본값」, 무제한이면 「무제한」. */
export function effectiveLimitText(effective: EffectiveUsageLimit): string {
  if (effective.source === "unlimited" || effective.value === null) return USAGE_LIMIT_SOURCE_LABEL.unlimited;
  return `${formatNumber(effective.value)} · ${USAGE_LIMIT_SOURCE_LABEL[effective.source]}`;
}

/** 1 MB = 1,048,576바이트 — 코어 기본값(50 MiB)·413 문구(「파일이 50MB를 초과합니다」)와 같은 단위다. */
export const MIB = 1024 * 1024;

/**
 * 바이트 → MB 숫자. 반올림하지 않는다 — 1MB 미만이나 MB로 나누어떨어지지 않는 값도 같은 바이트로 되돌아온다
 * (MIB가 2의 거듭제곱이라 나눗셈이 정확하다).
 */
export function bytesToMegabytes(bytes: number): number {
  return bytes / MIB;
}

/** MB → 바이트(반올림). `bounds`를 주면 그 범위로 맞춘다 — 업로드 재정의는 코어 범위 1바이트~하드 상한이다. */
export function megabytesToBytes(megabytes: number, bounds?: { min: number; max: number }): number {
  const bytes = Math.round(megabytes * MIB);
  return bounds ? Math.min(bounds.max, Math.max(bounds.min, bytes)) : bytes;
}

/**
 * 바이트 → 「50MB」. 1MB 미만은 소수 여섯째 자리까지 보여 0으로 뭉개지지 않는다(1바이트 = 「0.000001MB」), 그 밖은
 * 둘째 자리까지. 값이 없으면 「—」.
 */
export function formatMegabytes(bytes: number | null | undefined): string {
  if (typeof bytes !== "number" || !Number.isFinite(bytes)) return formatNumber(bytes);
  return `${formatNumber(bytes / MIB, { maximumFractionDigits: bytes < MIB ? 6 : 2 })}MB`;
}

/** 관리 화면의 정확한 크기 — MB로 나누어떨어지지 않으면 바이트를 함께 보인다(「0.5MB (524,288바이트)」). */
export function formatUploadSize(bytes: number): string {
  const megabytes = formatMegabytes(bytes);
  return bytes % MIB === 0 ? megabytes : `${megabytes} (${formatNumber(bytes)}바이트)`;
}

/**
 * 저장이 끝난 뒤 남길 편집 칸 — 보낸 값 그대로인 칸만 지우고(저장 응답이 새 기준이 된다), 저장 중에 다시 고친 칸은
 * 남긴다. 섹션 폼은 편집한 칸만 들고 있으므로, 편집하지 않은 칸은 재조회·저장 응답의 서버 값을 그대로 따른다.
 */
export function settleEdits<K extends string>(
  current: Partial<Record<K, string>>,
  submitted: Partial<Record<K, string>>,
): Partial<Record<K, string>> {
  const next = { ...current };
  for (const key of Object.keys(submitted) as K[]) {
    if (next[key] === submitted[key]) delete next[key];
  }
  return next;
}

export const INVALID_LIMIT_MESSAGE = "입력값이 올바르지 않습니다";

/** 저장 422 문구에 쓸 필드 정보 — 라벨과 범위 숫자 표시(기본은 천 단위 쉼표). */
export type LimitFieldInfo = { label: string; format?: (value: number) => string };

/**
 * 저장 실패가 검증 422면 항목별 문구 목록, 아니면 null(호출부가 토스트로 알린다).
 *
 * 범위 위반은 서버가 준 경계(`ctx.ge`·`ctx.le` 등)로 「채팅 질문: 100,000 이하여야 합니다」처럼 보이고, 그 밖은
 * 서버 `msg`를 붙인다. 검증 본문의 `input`은 쓰지 않는다(입력값을 되돌려줄 수 있다 — `lib/notify.ts`와 같은 규칙).
 */
export function limitValidationMessages(
  error: unknown,
  fields: Readonly<Record<string, LimitFieldInfo>>,
): string[] | null {
  if (!(error instanceof ApiError) || error.status !== 422) return null;
  const body = error.body;
  const detail = typeof body === "object" && body !== null ? (body as { detail?: unknown }).detail : undefined;
  if (typeof detail === "string" && detail.trim()) return [detail.trim()];
  if (!Array.isArray(detail) || detail.length === 0) return [INVALID_LIMIT_MESSAGE];
  return detail.map((item) => validationItemMessage(item, fields));
}

function validationItemMessage(item: unknown, fields: Readonly<Record<string, LimitFieldInfo>>): string {
  if (typeof item !== "object" || item === null) return INVALID_LIMIT_MESSAGE;
  const { loc, type, msg, ctx } = item as { loc?: unknown; type?: unknown; msg?: unknown; ctx?: unknown };
  const field = Array.isArray(loc) ? loc.filter((part): part is string => typeof part === "string").at(-1) : undefined;
  const info = field && Object.prototype.hasOwnProperty.call(fields, field) ? fields[field] : undefined;
  const label = info?.label ?? field ?? "입력값";
  const format = info?.format ?? ((value: number) => formatNumber(value));
  const bound = (key: string) => {
    const value = typeof ctx === "object" && ctx !== null ? (ctx as Record<string, unknown>)[key] : undefined;
    return typeof value === "number" ? format(value) : undefined;
  };
  const reason = (() => {
    switch (type) {
      case "greater_than_equal":
        return bound("ge") && `${bound("ge")} 이상이어야 합니다`;
      case "less_than_equal":
        return bound("le") && `${bound("le")} 이하여야 합니다`;
      case "greater_than":
        return bound("gt") && `${bound("gt")}보다 커야 합니다`;
      case "less_than":
        return bound("lt") && `${bound("lt")}보다 작아야 합니다`;
      case "int_type":
      case "int_parsing":
      case "int_from_float":
        return "정수여야 합니다";
      case "extra_forbidden":
        return "서버가 모르는 항목입니다";
      default:
        return undefined;
    }
  })();
  const text = reason || (typeof msg === "string" && msg.trim() ? msg.trim() : INVALID_LIMIT_MESSAGE);
  return `${label}: ${text}`;
}
