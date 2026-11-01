/**
 * 슬라이드 자료 생성 흐름(docs/plans/slide-deck-studio.md D-2·D-4) — 다이얼로그 → (개요 run → 검토 → 계속) → 덱 run.
 * 개요 검토는 코어 HITL 없이 run 두 개로 처리한다. 「계속」이 만드는 덱 run은 개요 run의 형식·길이·언어·설명과
 * 소스 핀을 그대로 쓰고, 정규화한 개요·의견·개요 run id를 params로 더한다. 덱 run은 의견이 없으면 개요의 장(검색어·
 * 근거 소스 포함)을 그대로 쓰고, 의견이 있으면 개요를 고친 뒤 쓴다(#218).
 */
import type { AgentRun } from "@/lib/agent-types";
import { serializeOutline, type SlideFormat, type SlideOutlineDoc } from "@/lib/slide-deck/parse";

export type SlideLength = "short" | "default";
export type SlideLanguage = "한국어" | "English";

/** 시드 params_schema(「슬라이드 자료」·「슬라이드 자료 개요」)의 공통 키와 같다 — test_seed_slide_deck.py. */
export interface SlideCommonParams {
  format: SlideFormat;
  length: SlideLength;
  language: SlideLanguage;
  description: string;
  /** 삽화 장수 — 0(끔) 또는 `SLIDE_IMAGE_MAX`(켬). 덱 loop의 `max_items`가 된다(core-v0.23.0-upgrade D1-A). */
  image_count: number;
}

/** 시드 `maxLength`와 같은 입력 상한 — 개요는 장마다 검색어·근거 소스가 더해져 4만 자다(#218, 시드 `SLIDE_OUTLINE_PARAM_MAX`) */
export const SLIDE_DESCRIPTION_MAX = 2000;
export const SLIDE_FEEDBACK_MAX = 2000;
export const SLIDE_OUTLINE_MAX = 40000;
/** 시드 `SLIDE_IMAGE_MAX`(image_count maximum)와 같다 — 「이미지 넣기」를 켜면 덱 하나에 이 수까지. */
export const SLIDE_IMAGE_MAX = 4;

export const DEFAULT_SLIDE_PARAMS: SlideCommonParams = {
  format: "detailed",
  length: "default",
  language: "한국어",
  description: "",
  image_count: 0,
};

/**
 * run params에서 공통 키만 꺼낸다 — 모르는 값은 기본값으로(서버 enum 검증을 통과하는 값만 넘긴다).
 * 삽화 장수가 없는 옛 개요 run은 0(이미지 없음)으로 계속한다.
 */
export function commonParamsOf(run: AgentRun): SlideCommonParams {
  const p = (run.params ?? {}) as Record<string, unknown>;
  return {
    format: p.format === "presenter" ? "presenter" : "detailed",
    length: p.length === "short" ? "short" : "default",
    language: p.language === "English" ? "English" : "한국어",
    description: typeof p.description === "string" ? p.description.slice(0, SLIDE_DESCRIPTION_MAX) : "",
    image_count:
      Number.isInteger(p.image_count) && (p.image_count as number) >= 0 && (p.image_count as number) <= SLIDE_IMAGE_MAX
        ? (p.image_count as number)
        : 0,
  };
}

/**
 * 「계속」이 만드는 덱 run 요청. 소스 핀(`input_source_ids`)은 개요 run 그대로 — null(노트 전체)이면 보내지 않는다.
 * 실행 모델(`model_id`, #216)도 개요 run 그대로 — null(기본 모델)이면 보내지 않는다.
 * 직렬화한 개요가 시드 상한을 넘으면 null — 서버가 422로 거절할 요청을 보내지 않는다(독립 리뷰 지적).
 * JS 길이(UTF-16)는 JSON Schema 길이(코드 포인트) 이상이라 이 검사가 보수적이다.
 *
 * 개요(최상위와 장마다)의 형식·언어를 개요 run의 값으로 덮어쓴다(#218 독립 리뷰 M). 시드 덱은 loop 공통 params(코어 #390)로
 * 형식·언어를 넘기지만, 그 전에 복제한 사용자 덱(옛 loop — 공통 params 없음)의 장 작성 본문은 장 항목에서 형식·언어를 읽는다.
 * 방안 ④ 전에 저장한 개요의 장에는 format·language가 없어 그대로 넘기면 발표자 슬라이드가 자세한 자료로 쓰인다 — 사용자
 * 복제본이 남아 있는 동안 유지한다. 시드 덱에서는 장 값이 run 설정과 같아 충돌하지 않는다.
 */
export function continueRequest(
  outlineRun: AgentRun,
  outline: SlideOutlineDoc,
  feedback: string,
): { params: Record<string, unknown>; source_ids?: string[]; model_id?: string } | null {
  const common = commonParamsOf(outlineRun);
  const slides = outline.slides.map((s) => ({ ...s, format: common.format, language: common.language }));
  const serialized = serializeOutline({ ...outline, format: common.format, slides });
  if (serialized.length > SLIDE_OUTLINE_MAX) return null;
  const sources = outlineRun.input_source_ids;
  const modelId = outlineRun.model_id;
  return {
    params: {
      ...common,
      outline: serialized,
      feedback: feedback.trim().slice(0, SLIDE_FEEDBACK_MAX),
      outline_run_id: outlineRun.id,
    },
    ...(sources ? { source_ids: sources } : {}),
    ...(modelId ? { model_id: modelId } : {}),
  };
}

/**
 * 이 개요 run으로 이미 「계속」한 덱 run — 목록(최근 50개) 안에서 찾는다(D-4). 없으면 undefined.
 * 실패·취소된 덱 run은 세지 않는다 — 다시 「계속」할 수 있다.
 */
export function continuedDeckRun(outlineRunId: string, runs: AgentRun[] | undefined): AgentRun | undefined {
  return (runs ?? []).find(
    (r) =>
      r.status !== "failed" &&
      r.status !== "cancelled" &&
      (r.params as Record<string, unknown> | null)?.outline_run_id === outlineRunId,
  );
}
