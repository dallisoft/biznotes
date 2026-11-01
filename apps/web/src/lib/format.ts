/**
 * 표시용 날짜·숫자·통화·기간 포맷 — 화면 표기의 단일 출처(plan §6.5 「포맷 유틸」, DP-7·D-15).
 *
 * 화면에 보이는 값을 만드는 `toLocale*`·`toFixed`는 이 모듈 안에만 둔다(AC-1.5). 입력 파싱처럼
 * 화면에 보이지 않는 용도는 phase 보고서의 허용 목록에 근거와 함께 남긴다.
 *
 * 공통 규칙: `null`·`undefined`·`NaN`·무한대·잘못된 날짜는 모두 {@link EMPTY}(「—」)로 표시한다.
 * 날짜·시각은 브라우저 시간대가 아니라 표시 시간대({@link DISPLAY_TIMEZONE}, KST)로 표시한다(#211) — 서버 렌더링과
 * 브라우저 렌더링, 해외 브라우저가 같은 시각을 보인다.
 */

import { DISPLAY_TIMEZONE } from "./timezones";

/** 값이 없거나 해석할 수 없을 때 보이는 자리표시. */
export const EMPTY = "—";

const LOCALE = "ko-KR";

const DATE_FORMAT = new Intl.DateTimeFormat(LOCALE, {
  year: "numeric",
  month: "numeric",
  day: "numeric",
  timeZone: DISPLAY_TIMEZONE,
});
const DATE_TIME_OPTIONS: Intl.DateTimeFormatOptions = {
  year: "numeric",
  month: "numeric",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
};
const DATE_TIME_FORMAT = new Intl.DateTimeFormat(LOCALE, {
  ...DATE_TIME_OPTIONS,
  timeZone: DISPLAY_TIMEZONE,
});
const DATE_TIME_SECONDS_FORMAT = new Intl.DateTimeFormat(LOCALE, {
  ...DATE_TIME_OPTIONS,
  second: "2-digit",
  timeZone: DISPLAY_TIMEZONE,
});
const TIME_FORMAT = new Intl.DateTimeFormat(LOCALE, {
  hour: "numeric",
  minute: "2-digit",
  timeZone: DISPLAY_TIMEZONE,
});
// 표시 시간대의 달력 날짜(연·월·일) 조각 — 「어제」·「N일 전」과 연도 표시가 브라우저 시간대에 흔들리지 않게 한다
const DISPLAY_DAY_PARTS_FORMAT = new Intl.DateTimeFormat("en-US", {
  year: "numeric",
  month: "numeric",
  day: "numeric",
  timeZone: DISPLAY_TIMEZONE,
});

const USD_FORMAT = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
/** `title`용 정확값 — 유효숫자 10자리까지(지수 표기 없이). */
const USD_EXACT_FORMAT = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumSignificantDigits: 10,
});

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

export type DateInput = string | number | Date | null | undefined;
export type NumberInput = number | null | undefined;

function toDate(value: DateInput): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const d = value instanceof Date ? new Date(value.getTime()) : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

// 표의 셀마다 부르므로 옵션별 포맷터를 한 번만 만든다
const numberFormats = new Map<string, Intl.NumberFormat>();
function cachedNumberFormat(key: string, options: Intl.NumberFormatOptions): Intl.NumberFormat {
  let format = numberFormats.get(key);
  if (!format) {
    format = new Intl.NumberFormat(LOCALE, options);
    numberFormats.set(key, format);
  }
  return format;
}

function isFiniteNumber(value: NumberInput): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** 날짜 — 「2026. 10. 12.」 */
export function formatDate(value: DateInput): string {
  const d = toDate(value);
  return d ? DATE_FORMAT.format(d) : EMPTY;
}

/**
 * 날짜와 시각 — 「2026. 10. 14. 오후 11:50」.
 * 감사 로그처럼 초까지 필요한 곳은 `{ seconds: true }` — 「2026. 10. 14. 오후 11:50:40」.
 */
export function formatDateTime(value: DateInput, options: { seconds?: boolean } = {}): string {
  const d = toDate(value);
  if (!d) return EMPTY;
  return (options.seconds ? DATE_TIME_SECONDS_FORMAT : DATE_TIME_FORMAT).format(d);
}

/** 시각만 — 「오후 11:50」. 같은 날의 항목처럼 날짜가 문맥으로 정해질 때 쓴다. */
export function formatTime(value: DateInput): string {
  const d = toDate(value);
  return d ? TIME_FORMAT.format(d) : EMPTY;
}

/** 표시 시간대의 연·월·일 — 월은 1부터. */
function displayDayParts(d: Date): { year: number; month: number; day: number } {
  const parts: Record<string, number> = {};
  for (const p of DISPLAY_DAY_PARTS_FORMAT.formatToParts(d)) {
    if (p.type === "year" || p.type === "month" || p.type === "day") parts[p.type] = Number(p.value);
  }
  return { year: parts.year, month: parts.month, day: parts.day };
}

/** 표시 시간대 달력 날짜의 순번(UTC 자정 ms) — 두 시각의 달력 날짜 차이를 셀 때만 쓴다. */
function displayDayIndex(d: Date): number {
  const { year, month, day } = displayDayParts(d);
  return Date.UTC(year, month - 1, day);
}

/** 표시 시간대의 올해 연도 — 저작권 표기처럼 연도만 보일 때 쓴다. `now`는 테스트용이다. */
export function displayYear(now: Date = new Date()): number {
  return displayDayParts(now).year;
}

/**
 * 상대 시간 — 지난 시각을 「방금 전 / 3분 전 / 2시간 전 / 어제 / 3일 전」으로 보인다.
 *
 * - 1분 미만 「방금 전」, 1시간 미만 「N분 전」, 24시간 미만 「N시간 전」(경과 시간 기준)
 * - 그보다 오래되면 표시 시간대(KST) 달력 날짜 차이로: 1일 「어제」, 2~6일 「N일 전」, **7일 이상은 {@link formatDate}**
 * - 미래 시각: 1분 이내(서버·브라우저 시계 차이)는 「방금 전」, 그보다 먼 미래는 {@link formatDate}
 *
 * `now`는 테스트에서 기준 시각을 고정할 때만 넘긴다.
 */
export function formatRelative(value: DateInput, now: Date = new Date()): string {
  const d = toDate(value);
  if (!d) return EMPTY;
  const diff = now.getTime() - d.getTime();
  if (diff < -MINUTE_MS) return DATE_FORMAT.format(d);
  if (diff < MINUTE_MS) return "방금 전";
  if (diff < HOUR_MS) return `${Math.floor(diff / MINUTE_MS)}분 전`;
  if (diff < DAY_MS) return `${Math.floor(diff / HOUR_MS)}시간 전`;
  const days = Math.round((displayDayIndex(now) - displayDayIndex(d)) / DAY_MS);
  if (days <= 1) return "어제";
  if (days < 7) return `${days}일 전`;
  return DATE_FORMAT.format(d);
}

/**
 * 숫자 — 천 단위 쉼표(「1,234,567」). 소수는 기본 최대 3자리까지 보인다.
 * `maximumFractionDigits`로 소수 자릿수 상한을 바꾼다.
 */
export function formatNumber(
  value: NumberInput,
  options: { maximumFractionDigits?: number } = {},
): string {
  if (!isFiniteNumber(value)) return EMPTY;
  const digits = options.maximumFractionDigits ?? 3;
  return cachedNumberFormat(`number:${digits}`, { maximumFractionDigits: digits }).format(value);
}

/** 토큰 수 — 정수로 반올림한 천 단위 쉼표(「12,345」). 단위 글자는 열 머리글·라벨이 맡는다. */
export function formatTokens(value: NumberInput): string {
  if (!isFiniteNumber(value)) return EMPTY;
  return formatNumber(Math.round(value), { maximumFractionDigits: 0 });
}

/** 비율(0~1) → 백분율 — `formatPercent(0.0345, 1)` = 「3.5%」, 기본은 정수(「98%」). */
export function formatPercent(ratio: NumberInput, fractionDigits = 0): string {
  if (!isFiniteNumber(ratio)) return EMPTY;
  return cachedNumberFormat(`percent:${fractionDigits}`, {
    style: "percent",
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(ratio);
}

function isBelowCent(value: number): boolean {
  return value > 0 && value < 0.01;
}

/**
 * 달러 금액 — 0은 「$0.00」, 0 < v < 0.01은 「< $0.01」, 그 밖은 소수 2자리(「$1,234.57」).
 * 「< $0.01」로 보일 때의 정확값은 {@link formatCurrencyUSDTitle}로 `title`에 넣는다.
 */
export function formatCurrencyUSD(value: NumberInput): string {
  if (!isFiniteNumber(value)) return EMPTY;
  if (isBelowCent(value)) return "< $0.01";
  return USD_FORMAT.format(value);
}

/**
 * {@link formatCurrencyUSD}가 「< $0.01」로 줄여 보일 때만 정확값(「$0.004」)을 돌려준다.
 * 그 밖에는 `undefined` — `title={formatCurrencyUSDTitle(v)}`로 그대로 넘기면 된다.
 */
export function formatCurrencyUSDTitle(value: NumberInput): string | undefined {
  if (!isFiniteNumber(value) || !isBelowCent(value)) return undefined;
  return USD_EXACT_FORMAT.format(value);
}

const SECONDS_FORMAT = new Intl.NumberFormat(LOCALE, {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

/** 기간(밀리초) — 1초 미만 「850ms」, 1초 이상 「17.9초」(소수 1자리, 천 단위 쉼표). */
export function formatDuration(ms: NumberInput): string {
  if (!isFiniteNumber(ms)) return EMPTY;
  const rounded = Math.round(ms);
  // 999.6ms처럼 반올림하면 1000ms가 되는 값은 초 단위로 보인다
  if (Math.abs(rounded) < 1000) return `${rounded}ms`;
  return `${SECONDS_FORMAT.format(ms / 1000)}초`;
}

/** 축약 단위 — 그래프 축처럼 좁은 자리에서 큰 수를 줄여 보인다(「50K」·「0.5M」·「1K」). */
export type CompactUnit = "" | "K" | "M" | "B";

const COMPACT_UNITS: readonly { unit: Exclude<CompactUnit, "">; size: number }[] = [
  { unit: "B", size: 1e9 },
  { unit: "M", size: 1e6 },
  { unit: "K", size: 1e3 },
];

/** 값의 크기에 맞는 축약 단위 — 1,000 미만은 단위 없음. */
export function compactUnitFor(value: NumberInput): CompactUnit {
  if (!isFiniteNumber(value)) return "";
  const abs = Math.abs(value);
  return COMPACT_UNITS.find((u) => abs >= u.size)?.unit ?? "";
}

/**
 * 축약 숫자 — 「50K」·「1.25M」·「999」(소수 최대 2자리). `unit`을 주면 그 단위로 맞춘다: 축 눈금은 가장 큰
 * 눈금의 단위를 함께 써서 「0, 0.5M, 1M」처럼 한 단위로 읽힌다(이슈 #219 참고 화면).
 */
export function formatCompact(value: NumberInput, unit: CompactUnit = compactUnitFor(value)): string {
  if (!isFiniteNumber(value)) return EMPTY;
  if (value === 0) return "0"; // 축의 원점은 단위 없이
  const size = COMPACT_UNITS.find((u) => u.unit === unit)?.size ?? 1;
  return `${formatNumber(value / size, { maximumFractionDigits: 2 })}${unit}`;
}

// 달력 날짜("YYYY-MM-DD")는 시각이 아니다 — UTC 자정으로 만들어 UTC로 표시해야 브라우저 시간대와 무관하게 같은 날이다.
const CALENDAR_DATE_FORMAT = new Intl.DateTimeFormat(LOCALE, {
  year: "numeric",
  month: "numeric",
  day: "numeric",
  timeZone: "UTC",
});
const CALENDAR_MONTH_DAY_FORMAT = new Intl.DateTimeFormat(LOCALE, {
  month: "numeric",
  day: "numeric",
  timeZone: "UTC",
});

function calendarDate(value: string | null | undefined): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value ?? "");
  if (!m) return null;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  return d.getUTCDate() === Number(m[3]) ? d : null;
}

/**
 * 서버가 준 달력 날짜("2026-10-27") — 「2026. 10. 27.」. 서버가 표시 시간대(KST)로 자른 날짜를 그대로 보인다
 * (`formatDate`처럼 브라우저 시간대로 옮기지 않는다).
 */
export function formatCalendarDate(value: string | null | undefined): string {
  const d = calendarDate(value);
  return d ? CALENDAR_DATE_FORMAT.format(d) : EMPTY;
}

/** 달력 날짜의 월·일만 — 「10. 27.」(그래프 x축 눈금). */
export function formatCalendarMonthDay(value: string | null | undefined): string {
  const d = calendarDate(value);
  return d ? CALENDAR_MONTH_DAY_FORMAT.format(d) : EMPTY;
}

/** 널리 쓰는 약어가 있는 시간대. 그 밖은 UTC 오프셋(「UTC-8」)으로 보인다. */
const TIME_ZONE_ABBR: Readonly<Record<string, string>> = {
  "Asia/Seoul": "KST",
  "Asia/Tokyo": "JST",
};

/**
 * 시간대 표시 — 「Asia/Seoul (KST)」, 「America/Los_Angeles (UTC-7)」, 「UTC」. 오프셋은 `at` 시점(기본 지금)의 값이다.
 * 해석할 수 없는 이름은 그대로 보인다.
 */
export function formatTimeZoneLabel(tz: string | null | undefined, at: Date = new Date()): string {
  if (!tz) return EMPTY;
  if (tz === "UTC" || tz === "Etc/UTC") return "UTC";
  if (Object.prototype.hasOwnProperty.call(TIME_ZONE_ABBR, tz)) return `${tz} (${TIME_ZONE_ABBR[tz]})`;
  try {
    const offset = new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "shortOffset" })
      .formatToParts(at)
      .find((p) => p.type === "timeZoneName")?.value;
    return offset ? `${tz} (${offset.replace(/^GMT/, "UTC")})` : tz;
  } catch {
    return tz;
  }
}
