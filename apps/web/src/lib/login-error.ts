import { ApiError } from "@/lib/api-client";
import { NETWORK_ERROR_MESSAGE } from "@/lib/notify";

/**
 * 로그인(`POST /auth/login`) 실패 문구 사전(#93).
 *
 * 코어는 401 「Invalid email or password」, 403 「Account is disabled」, 429(레이트 리밋)를 내고,
 * 스키마 검증(`EmailStr`·비밀번호 128자)은 FastAPI 422 목록을 낸다. 422 본문은 입력값을 되돌려줄 수
 * 있으므로(비밀번호 포함) 서버 메시지를 쓰지 않고 상태별 고정 문구만 보인다.
 */
export const LOGIN_ERROR_MESSAGE = {
  invalidCredentials: "이메일 또는 비밀번호가 올바르지 않습니다.",
  invalidInput: "이메일 또는 비밀번호 형식이 올바르지 않습니다.",
  disabled: "비활성화된 계정입니다. 관리자에게 문의하세요.",
  rateLimited: "로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.",
  failed: "잠시 후 다시 시도하세요.",
} as const;

const STATUS_MESSAGE: Readonly<Record<number, string>> = {
  401: LOGIN_ERROR_MESSAGE.invalidCredentials,
  403: LOGIN_ERROR_MESSAGE.disabled,
  422: LOGIN_ERROR_MESSAGE.invalidInput,
  429: LOGIN_ERROR_MESSAGE.rateLimited,
};

/** 로그인 실패 → 토스트 설명 한 줄. 서버 메시지는 어떤 경우에도 넣지 않는다. */
export function loginErrorMessage(err: unknown): string {
  if (err instanceof ApiError) return STATUS_MESSAGE[err.status] ?? LOGIN_ERROR_MESSAGE.failed;
  if (err instanceof TypeError) return NETWORK_ERROR_MESSAGE;
  return LOGIN_ERROR_MESSAGE.failed;
}
