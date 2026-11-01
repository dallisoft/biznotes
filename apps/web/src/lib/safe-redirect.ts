/**
 * 오픈 리다이렉트 방지 — `redirect` 쿼리 값을 앱 내부 경로로만 통과시키는 공용 헬퍼.
 *
 * 로그인 화면과 비밀번호 변경 화면이 같이 쓴다(PLAN AC-3.3, D-34′). 판정 단계:
 *  ① raw는 `/`로 시작하고 `//`로 시작하지 않으며 역슬래시·제어 문자(U+0000–U+001F, U+007F)가 없다
 *  ② `new URL(raw, origin)`의 origin이 현재 origin과 같다
 *  ③ 반환 후보 `r = pathname + search + hash`에 ①을 **다시** 적용한다 — URL 정규화가
 *     `/a/..//host`·`/%2e%2e//host`를 `//host`(scheme-relative)로 바꾸기 때문이다
 *  ④ `new URL(r, origin)`(실제 navigation과 같은 재해석)의 origin이 같을 때만 `r`을 돌려준다
 * 어느 단계든 실패하면 `/`. 퍼센트 인코딩(`%5C`·`%2F`)은 URL API가 디코드하지 않으므로
 * `/%5Cx`는 같은 origin의 경로로 남아 허용된다 — 판정 근거는 ④의 재해석 불변식이다.
 */

const FALLBACK = "/";
// 제어 문자(C0·DEL) — 찾는 것이 이 정규식의 목적이다
const CONTROL_CHARS = /[\u0000-\u001F\u007F]/;

function passesRawCheck(value: string): boolean {
  return (
    value.startsWith("/") &&
    !value.startsWith("//") &&
    !value.includes("\\") &&
    !CONTROL_CHARS.test(value)
  );
}

function currentOrigin(): string {
  return typeof window === "undefined" ? "http://localhost" : window.location.origin;
}

export function safeInternalPath(
  raw: string | null | undefined,
  origin: string = currentOrigin(),
): string {
  if (typeof raw !== "string" || !passesRawCheck(raw)) return FALLBACK;
  let candidate: string;
  try {
    const parsed = new URL(raw, origin);
    if (parsed.origin !== origin) return FALLBACK;
    candidate = parsed.pathname + parsed.search + parsed.hash;
    if (!passesRawCheck(candidate)) return FALLBACK;
    if (new URL(candidate, origin).origin !== origin) return FALLBACK;
  } catch {
    return FALLBACK;
  }
  return candidate;
}

/** 비밀번호 변경 화면 경로(앱 셸 밖, `/login`과 같은 레벨 — HCI-11 ①). */
export const CHANGE_PASSWORD_PATH = "/change-password";

/**
 * 변경 화면으로 보내는 href. 돌아갈 곳은 `safeInternalPath`를 거친 값만 싣고, `/`이거나 변경
 * 화면 자신이면 쿼리를 붙이지 않는다(중첩·루프 방지).
 */
export function changePasswordHref(returnTo: string | null | undefined): string {
  const target = safeInternalPath(returnTo);
  const targetPath = target.split(/[?#]/, 1)[0];
  return target === FALLBACK || targetPath === CHANGE_PASSWORD_PATH
    ? CHANGE_PASSWORD_PATH
    : `${CHANGE_PASSWORD_PATH}?redirect=${encodeURIComponent(target)}`;
}
