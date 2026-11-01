import { ApiError } from "@/lib/api-client";

/**
 * 비밀번호 규칙과 오류 문구 사전(코어 v0.11.0 #165 — `validate_new_password`와 같은 기준).
 *
 * 문구는 한국어 고정 사전이고 **입력값을 문구에 넣지 않는다**(PLAN T-17). 서버 오류도
 * `detail.code`·`detail.reason`만 보고 사전 문구로 바꾼다 — 서버 메시지·422 검증 본문을 그대로
 * 보이지 않는다(FastAPI 검증 422는 입력값을 되돌려줄 수 있다).
 */

export const PASSWORD_MIN_LENGTH = 8;
export const PASSWORD_MAX_BYTES = 72;

/** 코어 `len(plain)`과 같은 글자 수 — UTF-16 코드 유닛이 아니라 코드 포인트로 센다. */
export function passwordLength(value: string): number {
  return Array.from(value).length;
}

/** bcrypt 상한 판정용 UTF-8 바이트 수. */
export function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).length;
}

export type PasswordIssue =
  | "current_required"
  | "too_short"
  | "too_long_bytes"
  | "mismatch"
  | "same_as_current";

export const PASSWORD_ISSUE_MESSAGE: Record<PasswordIssue, string> = {
  current_required: "현재 비밀번호를 입력하세요.",
  too_short: `새 비밀번호는 ${PASSWORD_MIN_LENGTH}자 이상이어야 합니다.`,
  too_long_bytes: `새 비밀번호가 너무 깁니다(UTF-8 ${PASSWORD_MAX_BYTES}바이트 이하).`,
  mismatch: "새 비밀번호 확인이 일치하지 않습니다.",
  same_as_current: "새 비밀번호는 현재 비밀번호와 달라야 합니다.",
};

/** 제출 전 클라이언트 사전 검사 — 첫 위반 하나를 돌려주고, 없으면 null. */
export function checkPasswordChange(input: {
  current: string;
  next: string;
  confirm: string;
}): PasswordIssue | null {
  if (input.current.length === 0) return "current_required";
  if (passwordLength(input.next) < PASSWORD_MIN_LENGTH) return "too_short";
  if (utf8ByteLength(input.next) > PASSWORD_MAX_BYTES) return "too_long_bytes";
  if (input.next !== input.confirm) return "mismatch";
  if (input.next === input.current) return "same_as_current";
  return null;
}

/** 서버 422 `invalid_new_password`의 `reason` → 문구. 모르는 reason은 일반 문구. */
export const NEW_PASSWORD_REASON_MESSAGE: Record<string, string> = {
  too_short: PASSWORD_ISSUE_MESSAGE.too_short,
  too_long_bytes: PASSWORD_ISSUE_MESSAGE.too_long_bytes,
  same_as_current: PASSWORD_ISSUE_MESSAGE.same_as_current,
};

export const INVALID_NEW_PASSWORD_MESSAGE = "새 비밀번호가 규칙에 맞지 않습니다.";
export const INVALID_CURRENT_PASSWORD_MESSAGE = "현재 비밀번호가 올바르지 않습니다.";
export const RATE_LIMITED_MESSAGE = "요청이 너무 많습니다. 잠시 후 다시 시도하세요.";
export const CHANGE_FAILED_MESSAGE = "비밀번호를 변경하지 못했습니다. 잠시 후 다시 시도하세요.";
export const INVALID_INPUT_MESSAGE = "입력값이 올바르지 않습니다.";
/** 관리자가 정한 비밀번호(생성·재설정)의 다음 단계 안내(AC-3.5) — 강제 변경을 지원하는 API에서만 보인다. */
export const FIRST_LOGIN_CHANGE_NOTICE = "첫 로그인 때 비밀번호를 바꿔야 합니다";
/** 구 API(능력 없음) 안내 — AC-3.4 문언 그대로. */
export const PASSWORD_CHANGE_UNSUPPORTED_MESSAGE = "이 서버는 비밀번호 변경을 지원하지 않습니다";
/** 능력 판정 실패(네트워크 오류·5xx) — 지원·미지원 어느 쪽으로도 읽지 않는다. */
export const PASSWORD_SUPPORT_PROBE_FAILED_MESSAGE = "비밀번호 변경을 지원하는지 확인하지 못했습니다.";

function detailOf(err: ApiError): Record<string, unknown> | null {
  const body = err.body;
  if (typeof body !== "object" || body === null) return null;
  const detail = (body as { detail?: unknown }).detail;
  return typeof detail === "object" && detail !== null && !Array.isArray(detail)
    ? (detail as Record<string, unknown>)
    : null;
}

/** 422 `invalid_new_password`의 `reason`(없거나 다른 오류면 undefined). */
export function invalidNewPasswordReason(err: unknown): string | undefined {
  if (!(err instanceof ApiError) || err.status !== 422 || err.code !== "invalid_new_password") {
    return undefined;
  }
  const reason = detailOf(err)?.reason;
  return typeof reason === "string" ? reason : undefined;
}

/** 422 오류 → 문구. `invalid_new_password`면 reason 사전, 그 밖의 422는 입력값 없는 일반 문구. */
export function newPasswordErrorMessage(err: ApiError): string {
  if (err.code === "invalid_new_password") {
    const reason = invalidNewPasswordReason(err);
    return (reason && NEW_PASSWORD_REASON_MESSAGE[reason]) || INVALID_NEW_PASSWORD_MESSAGE;
  }
  return INVALID_INPUT_MESSAGE;
}

export type PasswordChangeFailure =
  | { kind: "message"; message: string }
  | { kind: "login" }
  | { kind: "unsupported" };

/**
 * `POST /auth/password` 실패 → 화면 처리(AC-3.3·AC-3.4).
 * 401은 로그인으로, 404·405는 미지원 안내로(재시도하지 않는다), 나머지는 사전 문구.
 */
export function describePasswordChangeError(err: unknown): PasswordChangeFailure {
  if (!(err instanceof ApiError)) return { kind: "message", message: CHANGE_FAILED_MESSAGE };
  if (err.status === 401) return { kind: "login" };
  if (err.status === 404 || err.status === 405) return { kind: "unsupported" };
  if (err.status === 400 && err.code === "invalid_current_password") {
    return { kind: "message", message: INVALID_CURRENT_PASSWORD_MESSAGE };
  }
  if (err.status === 422) return { kind: "message", message: newPasswordErrorMessage(err) };
  if (err.status === 429) return { kind: "message", message: RATE_LIMITED_MESSAGE };
  return { kind: "message", message: CHANGE_FAILED_MESSAGE };
}
