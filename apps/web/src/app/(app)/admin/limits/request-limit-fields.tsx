"use client";

import { FormField, FormSection } from "@/components/layout/form-section";
import { Input } from "@/components/ui/input";
import type { LimitSettings } from "@/lib/admin-types";

interface LimitFieldSpec {
  key: keyof LimitSettings;
  label: string;
  unit?: string;
  min: number;
  max: number;
  step: number;
  /** 저장값이 없을 때 서버가 쓰는 값 */
  defaultValue: number;
  /** 금액처럼 소수 자릿수를 고정해 보이는 항목 */
  fractionDigits?: number;
  help: string;
}

/**
 * 「요청당 한도」 항목의 기본값·범위 — 서버 `app/services/limits.py`의 `LIMIT_BOUNDS`와 같다
 * (`tests/test_admin_limits.py::test_limit_bounds_match_web_form`이 서버 숫자를 고정한다).
 * 상한은 코어 하드 상한이다 — 넘기면 코어가 조용히 깎아 화면 값과 어긋나므로 서버가 422로 거절한다.
 */
export const LIMIT_FIELD_GROUPS: Array<{
  title: string;
  description: string;
  fields: LimitFieldSpec[];
}> = [
  {
    title: "채팅 1회 한도",
    description: "자율 모드 질문 1건이 도구 호출과 답변 생성에 쓰는 양을 모두 셉니다.",
    fields: [
      {
        key: "chat_token_budget",
        label: "1회 토큰 한도",
        unit: "토큰",
        min: 10_000,
        max: 200_000,
        step: 1000,
        defaultValue: 100_000,
        help: "넘으면 답변 대신 「채팅 1회 토큰 한도」 오류를 보여 줍니다. 원문을 많이 읽는 질문은 한도를 넉넉히 두세요.",
      },
      {
        key: "chat_cost_budget_usd",
        label: "1회 비용 한도",
        unit: "USD",
        min: 0.05,
        max: 2,
        step: 0.01,
        defaultValue: 0.5,
        fractionDigits: 2,
        help: "모델에 등록한 단가로 셉니다 — 단가가 없는 모델에는 걸리지 않습니다. 넘으면 「채팅 1회 비용 한도」 오류를 보여 줍니다.",
      },
      {
        key: "chat_max_iters",
        label: "최대 도구 반복",
        unit: "회",
        min: 1,
        max: 12,
        step: 1,
        defaultValue: 6,
        help: "다 쓰면 도구를 더 부르지 않고 그때까지 찾은 내용으로 답합니다.",
      },
    ],
  },
  {
    title: "채팅 답변",
    description: "일반 모드와 자율 모드에 함께 쓰입니다.",
    fields: [
      {
        key: "chat_max_tokens",
        label: "턴당 출력 토큰",
        unit: "토큰",
        min: 512,
        max: 16_384,
        step: 256,
        defaultValue: 4096,
        help: "답변 한 번의 최대 길이입니다. 표처럼 긴 답변이 잘리면 올리세요 — 모델의 출력 한도보다 크면 호출이 실패합니다. 자율 모드에서 도구 반복을 다 쓴 뒤의 마무리 답변은 코어 수정 전까지 2,048토큰으로 나갑니다.",
      },
      {
        key: "chat_history_messages",
        label: "대화 이력 길이",
        unit: "개",
        min: 0,
        max: 20,
        step: 1,
        defaultValue: 6,
        help: "질문과 함께 보내는 최근 메시지 수입니다(질문·답변 각 1개). 0이면 이전 대화 없이 답합니다.",
      },
    ],
  },
  {
    title: "원문 읽기",
    description: "자율 모드가 source_load 도구로 소스 원문을 한 번에 읽는 양입니다.",
    fields: [
      {
        key: "source_load_max_sources",
        label: "1회 최대 소스 수",
        unit: "개",
        min: 1,
        max: 20,
        step: 1,
        defaultValue: 5,
        help: "넘는 소스는 읽지 않고 읽지 않았다는 사실을 모델에 알립니다.",
      },
      {
        key: "source_load_max_chars",
        label: "1회 최대 글자 수",
        unit: "자",
        min: 5_000,
        max: 200_000,
        step: 1000,
        defaultValue: 60_000,
        help: "한 번에 읽는 원문 전체의 길이입니다.",
      },
      {
        key: "source_load_budget_ratio",
        label: "남은 토큰 한도 비율",
        min: 0,
        max: 1,
        step: 0.05,
        defaultValue: 0.3,
        help: "원문을 남은 1회 토큰 한도에 이 비율을 곱한 만큼으로 줄입니다. 0이면 끕니다.",
      },
    ],
  },
  {
    title: "에이전트 승인",
    description: "승인이 필요한 도구 호출(HITL)을 기다리는 시간입니다.",
    fields: [
      {
        key: "approval_timeout_minutes",
        label: "승인 대기 시간",
        unit: "분",
        min: 1,
        max: 1_440,
        step: 1,
        defaultValue: 10,
        help: "이 시간 안에 승인하지 않으면 요청이 만료되고 실행은 실패합니다. 바꾼 값은 새 승인 요청부터 적용됩니다.",
      },
    ],
  },
];

const LIMIT_FIELDS = LIMIT_FIELD_GROUPS.flatMap((group) => group.fields);

function formatLimit(value: number, fractionDigits?: number): string {
  return value.toLocaleString("ko-KR", { minimumFractionDigits: fractionDigits });
}

/** 「범위 …」 뒤에 붙는 문구 — 범위와 기본값을 함께 보인다(AC-10). */
function rangeText(spec: LimitFieldSpec): string {
  const f = (v: number) => formatLimit(v, spec.fractionDigits);
  return `${f(spec.min)}~${f(spec.max)} · 기본값 ${f(spec.defaultValue)}`;
}

/** 기준값과 다른 항목만 — 서버는 보낸 항목만 저장하므로 그사이 다른 관리자가 바꾼 항목을 덮어쓰지 않는다. */
export function changedLimits(form: LimitSettings, baseline: LimitSettings): Partial<LimitSettings> {
  const changes: Partial<LimitSettings> = {};
  for (const { key } of LIMIT_FIELDS) {
    if (!Object.is(form[key], baseline[key])) changes[key] = form[key];
  }
  return changes;
}

export function RequestLimitFields({
  form,
  onChange,
}: {
  form: LimitSettings;
  onChange: (next: LimitSettings) => void;
}) {
  return LIMIT_FIELD_GROUPS.map((group) => (
    <FormSection
      key={group.title}
      headingLevel={3}
      title={group.title}
      description={group.description}
    >
      {group.fields.map((spec) => (
        <FormField
          key={spec.key}
          label={spec.label}
          unit={spec.unit}
          range={rangeText(spec)}
          help={spec.help}
        >
          {(field) => (
            <Input
              {...field}
              type="number"
              required
              step={spec.step}
              min={spec.min}
              max={spec.max}
              value={form[spec.key]}
              onChange={(e) => onChange({ ...form, [spec.key]: +e.target.value })}
            />
          )}
        </FormField>
      ))}
    </FormSection>
  ));
}
