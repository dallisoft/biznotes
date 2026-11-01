/**
 * 시간대 상수와 라벨(#213, 코어 v0.24.0 #351·#352).
 *
 * 배포 표시 시간대는 API·워커의 `DISPLAY_TIMEZONE`(compose `api-env`·Helm ConfigMap)과 같은 `Asia/Seoul`이다 — 일일
 * 사용량 한도의 「오늘」이 이 값을 따른다. 웹은 이 설정을 서버에서
 * 받지 않으므로 여기 한 곳에만 둔다. 배포 값을 바꾸면 함께 바꾼다. 한국 표준시는 서머타임이 없어 UTC 오프셋이
 * `+09:00`으로 고정이다.
 */

/** 한국 표준시의 IANA 이름 — 코어는 대소문자까지 정확한 이름만 받는다(`asia/seoul`·`KST`는 422). */
export const KST = "Asia/Seoul";

/** 배포 표시 시간대(`DISPLAY_TIMEZONE`). */
export const DISPLAY_TIMEZONE = KST;

/** 화면 문구용 표시 시간대 이름. */
export const DISPLAY_TIMEZONE_NAME = "한국 표준시(KST)";

/** 표시 시간대의 UTC 오프셋 — 서머타임이 없어 고정이다. */
const DISPLAY_TIMEZONE_OFFSET = "+09:00";

/**
 * `<input type="date">`의 "YYYY-MM-DD" → 표시 시간대 그날 00:00의 ISO 문자열. `new Date(str)` 뒤 `setHours()`로
 * 바꾸면 브라우저 시간대 기준이 되므로 문자열로 조립한다.
 */
export function displayDayStart(date: string): string {
  return `${date}T00:00:00.000${DISPLAY_TIMEZONE_OFFSET}`;
}

/** "YYYY-MM-DD" → 표시 시간대 그날 23:59:59.999의 ISO 문자열({@link displayDayStart}와 같은 방식). */
export function displayDayEnd(date: string): string {
  return `${date}T23:59:59.999${DISPLAY_TIMEZONE_OFFSET}`;
}
