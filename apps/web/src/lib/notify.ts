/**
 * 토스트 알림 — sonner 래퍼(plan §6.5 「토스트」).
 *
 * - 성공: 「{대상}을(를) {동작}했습니다」(예: 「노트북을 만들었습니다」), 4초. 「되돌리기」 같은 버튼이 있으면 8초.
 * - 실패: 제목 「{동작}하지 못했습니다」, 6초. 설명은 알려진 오류 코드·HTTP 상태면 한국어 사전 문구,
 *   모르면 「잠시 후 다시 시도해 주세요」이고 서버 메시지는 **설명 둘째 줄로만** 붙인다.
 *
 * **비밀값 토스트 금지**: 비밀번호·토큰·시크릿 같은 비밀값은 토스트에 넣지 않는다 — 한 번만 보여 줄
 * 비밀값은 결과 다이얼로그(`SecretRevealDialog`, P5)로 보인다. 이 모듈은 실수를 줄이는 방어선으로
 * ① 422 응답의 서버 메시지는 붙이지 않고(FastAPI 검증 본문은 입력값을 되돌려줄 수 있다 — 비밀번호 포함)
 * ② 모든 토스트 문자열에서 비밀값 모양(`password=…`, `"token": "…"`, `Bearer …`, JWT)을 가린다.
 * 키 없이 값만 있는 무작위 문자열은 가려낼 수 없으므로, 방어선이 아니라 호출 규칙이 1차 보장이다.
 */
import { toast } from "sonner";
import { ApiError } from "@/lib/api-client";

export const SUCCESS_DURATION_MS = 4000;
export const ERROR_DURATION_MS = 6000;
/** 버튼(「되돌리기」 등)이 달린 성공 토스트 — 읽고 누를 시간을 더 준다(#220). */
export const ACTION_DURATION_MS = 8000;
export const FALLBACK_ERROR_DESCRIPTION = "잠시 후 다시 시도해 주세요";
/** 가려진 비밀값 자리 */
export const REDACTED = "••••";
/** 서버 메시지는 이 길이까지만 보인다(긴 스택·본문이 토스트를 덮지 않게). */
export const SERVER_MESSAGE_MAX_LENGTH = 200;

/** 본문 `detail.code` → 문구. 코어가 기계 판독용 코드로 내보내는 오류만 둔다. */
export const ERROR_CODE_MESSAGE: Readonly<Record<string, string>> = {
  password_change_required: "비밀번호를 먼저 변경해야 합니다.",
  invalid_current_password: "현재 비밀번호가 올바르지 않습니다.",
  invalid_new_password: "새 비밀번호가 규칙에 맞지 않습니다.",
  password_policy_rejected_temporary: "임시 비밀번호를 만들지 못했습니다. 다시 시도해 주세요.",
};

/** 코드가 없을 때 HTTP 상태 → 문구 */
export const ERROR_STATUS_MESSAGE: Readonly<Record<number, string>> = {
  401: "로그인이 만료되었습니다. 다시 로그인해 주세요.",
  403: "이 작업을 할 권한이 없습니다.",
  404: "대상을 찾을 수 없습니다. 이미 삭제되었을 수 있습니다.",
  409: "다른 변경과 충돌했습니다. 새로 고친 뒤 다시 시도해 주세요.",
  413: "파일이나 요청이 너무 큽니다.",
  429: "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
};

export const NETWORK_ERROR_MESSAGE = "서버에 연결하지 못했습니다. 네트워크 연결을 확인해 주세요.";

// 키 이름 앞은 영숫자·밑줄이 아니어야 한다(`max_tokens=4096` 같은 설정 이름 오탐 방지).
// 값은 글자를 하나 이상 포함해야 한다(숫자만인 값은 개수·한도로 본다). lookbehind는 구형 Safari에서
// 정규식 파싱 오류를 내므로 쓰지 않는다.
const KEY_VALUE_SECRET =
  /(^|[^A-Za-z0-9_])((?:temporary_|new_|current_)?password|passwd|secret|token|api[_-]?key|access[_-]?key|비밀번호|시크릿|토큰)(["']?\s*[:=]\s*["']?)(?=[^\s"',;]*[A-Za-z])([^\s"',;]{4,})/gi;
const BEARER_SECRET = /\b(Bearer\s+)[A-Za-z0-9._~+/-]{8,}=*/gi;
const JWT_SECRET = /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g;

/** 토스트 문자열의 비밀값 모양을 {@link REDACTED}로 가린다. 키 이름·구분자는 남긴다. */
export function redactSecrets(text: string): string {
  return text
    .replace(JWT_SECRET, REDACTED)
    .replace(BEARER_SECRET, `$1${REDACTED}`)
    .replace(KEY_VALUE_SECRET, `$1$2$3${REDACTED}`);
}

/** 받침이 있으면 「을」, 없으면 「를」. 한글로 끝나지 않으면(영문·숫자) 「을(를)」. */
export function objectParticle(word: string): string {
  const last = word.trim().slice(-1);
  const code = last.charCodeAt(0);
  if (Number.isNaN(code) || code < 0xac00 || code > 0xd7a3) return "을(를)";
  return (code - 0xac00) % 28 === 0 ? "를" : "을";
}

/**
 * 성공 문구 — `successMessage("노트북", "삭제")` = 「노트북을 삭제했습니다」.
 * 「하다」로 끝나지 않는 동작(만들다 등)은 문장을 직접 써서 {@link notifySuccess}에 넘긴다.
 */
export function successMessage(target: string, action: string): string {
  return `${target}${objectParticle(target)} ${action}했습니다`;
}

/**
 * 실패 제목 — `failureTitle("저장")` = 「저장하지 못했습니다」, `failureTitle("노트북을 삭제")` =
 * 「노트북을 삭제하지 못했습니다」. `action`은 「하다」를 붙일 수 있는 말이어야 한다.
 */
export function failureTitle(action: string): string {
  return `${action}하지 못했습니다`;
}

function truncate(text: string): string {
  return text.length > SERVER_MESSAGE_MAX_LENGTH
    ? `${text.slice(0, SERVER_MESSAGE_MAX_LENGTH)}…`
    : text;
}

/** 오류에서 사람이 읽을 서버 메시지만 꺼낸다. 422 검증 본문·구조화 본문(배열 등)은 쓰지 않는다. */
function serverMessage(error: unknown): string | undefined {
  if (error instanceof ApiError) {
    if (error.status === 422) return undefined;
    const body = error.body;
    if (typeof body === "object" && body !== null && "detail" in body) {
      const detail = (body as { detail?: unknown }).detail;
      if (typeof detail === "string") return detail.trim() || undefined;
      if (typeof detail === "object" && detail !== null && !Array.isArray(detail)) {
        const message = (detail as { message?: unknown }).message;
        return typeof message === "string" && message.trim() ? message.trim() : undefined;
      }
      return undefined;
    }
    // 본문 없는 응답·`detail` 키 없는 본문은 api-client가 statusText를 넣는다.
    return typeof error.detail === "string" ? error.detail.trim() || undefined : undefined;
  }
  if (error instanceof Error) return error.message.trim() || undefined;
  if (typeof error === "string") return error.trim() || undefined;
  return undefined;
}

function isNetworkError(error: unknown): boolean {
  return (
    error instanceof TypeError && /failed to fetch|networkerror|load failed|network/i.test(error.message)
  );
}

/**
 * 실패 토스트의 설명 줄. 알려진 코드·상태·네트워크 오류는 사전 문구 한 줄, 그 밖은
 * [「잠시 후 다시 시도해 주세요」, 서버 메시지?]. 모든 줄은 비밀값을 가린 뒤 돌려준다.
 */
export function describeError(error: unknown): string[] {
  if (error instanceof ApiError) {
    if (error.code && Object.prototype.hasOwnProperty.call(ERROR_CODE_MESSAGE, error.code)) {
      return [ERROR_CODE_MESSAGE[error.code]];
    }
    if (Object.prototype.hasOwnProperty.call(ERROR_STATUS_MESSAGE, error.status)) {
      return [ERROR_STATUS_MESSAGE[error.status]];
    }
  } else if (isNetworkError(error)) {
    return [NETWORK_ERROR_MESSAGE];
  }
  const lines = [FALLBACK_ERROR_DESCRIPTION];
  const message = serverMessage(error);
  if (message && message !== FALLBACK_ERROR_DESCRIPTION) lines.push(redactSecrets(truncate(message)));
  return lines;
}

export interface NotifyOptions {
  /** 같은 id의 토스트를 갈아 끼울 때(진행 중 → 완료) */
  id?: string | number;
  /** 성공 토스트의 보조 설명(비밀값 금지) */
  description?: string;
  /** 성공 토스트의 버튼 — 확인 대화상자 대신 「되돌리기」를 둘 때(#220) */
  action?: { label: string; onClick: () => void };
}

/**
 * 성공 토스트(4초, 버튼이 있으면 8초). `message`는 완성된 문장 — {@link successMessage}로 만들 수 있다.
 */
export function notifySuccess(message: string, options: NotifyOptions = {}): string | number {
  return toast.success(redactSecrets(message), {
    id: options.id,
    duration: options.action ? ACTION_DURATION_MS : SUCCESS_DURATION_MS,
    description: options.description === undefined ? undefined : redactSecrets(options.description),
    action: options.action,
  });
}

/**
 * 실패 토스트(6초) — 제목 「{action}하지 못했습니다」, 설명은 {@link describeError}.
 * 두 줄 설명은 줄바꿈을 살려(`whitespace-pre-line`) 보인다.
 */
export function notifyError(
  action: string,
  error?: unknown,
  options: Pick<NotifyOptions, "id"> = {},
): string | number {
  return toast.error(redactSecrets(failureTitle(action)), {
    id: options.id,
    duration: ERROR_DURATION_MS,
    description: describeError(error).join("\n"),
    classNames: { description: "whitespace-pre-line" },
  });
}
