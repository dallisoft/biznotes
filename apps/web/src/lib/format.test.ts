import { describe, expect, it } from "vitest";
import {
  compactUnitFor,
  EMPTY,
  formatCalendarDate,
  formatCalendarMonthDay,
  formatCompact,
  formatCurrencyUSD,
  formatCurrencyUSDTitle,
  formatDate,
  formatDateTime,
  formatDuration,
  formatNumber,
  formatPercent,
  formatRelative,
  formatTime,
  formatTimeZoneLabel,
  formatTokens,
  displayYear,
} from "./format";

// 날짜·시각은 표시 시간대(KST)로 보인다(#211) — 입력은 UTC 순간으로 주고, 실행 환경(TZ)과 무관하게 KST 표기를 기대한다.
/** KST 벽시계 → 그 순간(Date). `kst(2026, 10, 14, 23, 50)` = 2026-10-14T14:50Z. 월은 1부터. */
function kst(year: number, month: number, day: number, hour = 0, minute = 0, second = 0): Date {
  return new Date(Date.UTC(year, month - 1, day, hour - 9, minute, second));
}

const OCT_14_2350_40 = kst(2026, 10, 14, 23, 50, 40);

describe("formatDate (AC-1.5)", () => {
  it("「2026. 10. 12.」 형식으로 보인다", () => {
    expect(formatDate(kst(2026, 10, 12, 8, 0))).toBe("2026. 10. 12.");
    expect(formatDate(kst(2026, 1, 5))).toBe("2026. 1. 5.");
  });

  it("ISO 문자열·epoch 밀리초도 받는다", () => {
    const d = kst(2026, 10, 12, 8, 0);
    expect(formatDate(d.toISOString())).toBe("2026. 10. 12.");
    expect(formatDate(d.getTime())).toBe("2026. 10. 12.");
  });

  it("KST 날짜로 자른다 — UTC로는 전날 오후인 순간도 KST 다음 날이다 (#211)", () => {
    expect(formatDate("2026-10-26T16:00:00Z")).toBe("2026. 10. 27.");
    expect(formatDate("2026-10-26T14:59:59Z")).toBe("2026. 10. 26.");
  });

  it("null·undefined·빈 문자열·잘못된 ISO는 「—」", () => {
    expect(formatDate(null)).toBe(EMPTY);
    expect(formatDate(undefined)).toBe(EMPTY);
    expect(formatDate("")).toBe(EMPTY);
    expect(formatDate("2026-13-45T99:99:99Z")).toBe(EMPTY);
    expect(formatDate("not-a-date")).toBe(EMPTY);
    expect(formatDate(new Date(Number.NaN))).toBe(EMPTY);
    expect(EMPTY).toBe("—");
  });
});

describe("formatDateTime (AC-1.5)", () => {
  it("「2026. 10. 14. 오후 11:50」 형식(초 없음)", () => {
    expect(formatDateTime(OCT_14_2350_40)).toBe("2026. 10. 14. 오후 11:50");
  });

  it("감사 로그용 seconds 옵션은 초를 포함한다", () => {
    expect(formatDateTime(OCT_14_2350_40, { seconds: true })).toBe("2026. 10. 14. 오후 11:50:40");
  });

  it("오전 시각과 한 자리 분", () => {
    expect(formatDateTime(kst(2026, 1, 5, 9, 5))).toBe("2026. 1. 5. 오전 9:05");
  });

  it("UTC 순간을 KST 벽시계로 보인다 (#211 수락 기준)", () => {
    expect(formatDateTime("2026-10-26T16:00:00Z")).toBe("2026. 10. 27. 오전 1:00");
  });

  it("null·잘못된 ISO는 「—」", () => {
    expect(formatDateTime(null)).toBe(EMPTY);
    expect(formatDateTime("2026-02-30T25:00:00Z")).toBe(EMPTY);
    expect(formatDateTime(undefined, { seconds: true })).toBe(EMPTY);
  });
});

describe("formatTime", () => {
  it("「오후 11:50」 형식으로 시각만 보인다", () => {
    expect(formatTime(OCT_14_2350_40)).toBe("오후 11:50");
    expect(formatTime(kst(2026, 10, 14, 0, 7))).toBe("오전 12:07");
  });

  it("잘못된 값은 「—」", () => {
    expect(formatTime(null)).toBe(EMPTY);
    expect(formatTime("garbage")).toBe(EMPTY);
  });
});

describe("formatRelative (AC-1.5 — 미래 시각·7일 경계)", () => {
  const now = kst(2026, 10, 14, 15, 0, 0);
  const ago = (ms: number) => new Date(now.getTime() - ms);

  it("1분 미만은 「방금 전」", () => {
    expect(formatRelative(now, now)).toBe("방금 전");
    expect(formatRelative(ago(59_999), now)).toBe("방금 전");
  });

  it("1시간 미만은 「N분 전」", () => {
    expect(formatRelative(ago(60_000), now)).toBe("1분 전");
    expect(formatRelative(ago(3 * 60_000), now)).toBe("3분 전");
    expect(formatRelative(ago(59 * 60_000 + 59_000), now)).toBe("59분 전");
  });

  it("24시간 미만은 「N시간 전」(자정을 넘어도 경과 시간 기준)", () => {
    expect(formatRelative(ago(2 * 3_600_000), now)).toBe("2시간 전");
    expect(formatRelative(ago(23 * 3_600_000 + 59 * 60_000), now)).toBe("23시간 전");
    const earlyMorning = kst(2026, 10, 14, 1, 0);
    expect(formatRelative(kst(2026, 10, 13, 23, 0), earlyMorning)).toBe("2시간 전");
  });

  it("24시간 이상이고 달력상 하루 전이면 「어제」", () => {
    expect(formatRelative(kst(2026, 10, 13, 14, 0), now)).toBe("어제");
    expect(formatRelative(kst(2026, 10, 13, 0, 0), now)).toBe("어제");
  });

  it("달력상 2~6일 전은 「N일 전」", () => {
    expect(formatRelative(kst(2026, 10, 12, 23, 59), now)).toBe("2일 전");
    expect(formatRelative(kst(2026, 10, 8, 0, 1), now)).toBe("6일 전");
  });

  it("달력 날짜는 KST 기준이다 — UTC로는 같은 날이어도 KST 자정을 넘으면 「어제」 (#211)", () => {
    // KST 10-15 00:30(= UTC 10-14 15:30)에서 본 KST 10-13 23:00(= UTC 10-13 14:00)은 KST 달력으로 2일 전
    const afterMidnight = kst(2026, 10, 15, 0, 30);
    expect(formatRelative(kst(2026, 10, 13, 23, 0), afterMidnight)).toBe("2일 전");
    expect(formatRelative(kst(2026, 10, 14, 0, 10), afterMidnight)).toBe("어제");
  });

  it("7일 경계: 달력상 7일 전부터는 formatDate", () => {
    expect(formatRelative(kst(2026, 10, 7, 23, 59), now)).toBe("2026. 10. 7.");
    expect(formatRelative(ago(7 * 86_400_000), now)).toBe("2026. 10. 7.");
    expect(formatRelative(kst(2025, 12, 31, 12, 0), now)).toBe("2025. 12. 31.");
  });

  it("미래 시각: 1분 이내 시계 차이는 「방금 전」, 그보다 먼 미래는 formatDate", () => {
    expect(formatRelative(new Date(now.getTime() + 30_000), now)).toBe("방금 전");
    expect(formatRelative(new Date(now.getTime() + 60_000), now)).toBe("방금 전");
    expect(formatRelative(new Date(now.getTime() + 60_001), now)).toBe("2026. 10. 14.");
    expect(formatRelative(kst(2026, 10, 20, 9, 0), now)).toBe("2026. 10. 20.");
  });

  it("null·잘못된 ISO는 「—」", () => {
    expect(formatRelative(null, now)).toBe(EMPTY);
    expect(formatRelative("yesterday", now)).toBe(EMPTY);
  });

  it("now를 넘기지 않으면 현재 시각 기준이다", () => {
    expect(formatRelative(new Date())).toBe("방금 전");
  });
});

describe("displayYear (#211 — 로그인 저작권 연도)", () => {
  it("KST 연도다 — UTC로는 아직 12월 31일이어도 KST 1월 1일이면 새해", () => {
    expect(displayYear(new Date("2026-12-31T15:00:00Z"))).toBe(2027);
    expect(displayYear(new Date("2026-12-31T14:59:59Z"))).toBe(2026);
  });
});

describe("formatNumber (AC-1.5)", () => {
  it("천 단위 쉼표", () => {
    expect(formatNumber(0)).toBe("0");
    expect(formatNumber(1234567)).toBe("1,234,567");
    expect(formatNumber(-9876)).toBe("-9,876");
  });

  it("소수는 기본 3자리까지, 옵션으로 상한 변경", () => {
    expect(formatNumber(12.345)).toBe("12.345");
    expect(formatNumber(0.004)).toBe("0.004");
    expect(formatNumber(12.345, { maximumFractionDigits: 1 })).toBe("12.3");
  });

  it("null·NaN·무한대는 「—」", () => {
    expect(formatNumber(null)).toBe(EMPTY);
    expect(formatNumber(undefined)).toBe(EMPTY);
    expect(formatNumber(Number.NaN)).toBe(EMPTY);
    expect(formatNumber(Number.POSITIVE_INFINITY)).toBe(EMPTY);
  });
});

describe("formatTokens", () => {
  it("정수 반올림 + 천 단위 쉼표", () => {
    expect(formatTokens(0)).toBe("0");
    expect(formatTokens(1234567)).toBe("1,234,567");
    expect(formatTokens(12.345)).toBe("12");
    expect(formatTokens(1499.5)).toBe("1,500");
  });

  it("null은 「—」", () => {
    expect(formatTokens(null)).toBe(EMPTY);
    expect(formatTokens(Number.NaN)).toBe(EMPTY);
  });
});

describe("formatPercent", () => {
  it("비율을 백분율로, 기본 정수", () => {
    expect(formatPercent(0)).toBe("0%");
    expect(formatPercent(0.985)).toBe("99%");
    expect(formatPercent(1)).toBe("100%");
  });

  it("소수 자릿수 지정", () => {
    expect(formatPercent(0.0345, 1)).toBe("3.5%");
    expect(formatPercent(0.004, 1)).toBe("0.4%");
  });

  it("null은 「—」", () => {
    expect(formatPercent(null)).toBe(EMPTY);
    expect(formatPercent(Number.NaN, 1)).toBe(EMPTY);
  });
});

describe("formatCurrencyUSD (AC-1.5 — 0·0.004·0.01·12.345·null)", () => {
  it("0은 「$0.00」", () => {
    expect(formatCurrencyUSD(0)).toBe("$0.00");
    expect(formatCurrencyUSDTitle(0)).toBeUndefined();
  });

  it("0 < v < 0.01은 「< $0.01」 + title에 정확값", () => {
    expect(formatCurrencyUSD(0.004)).toBe("< $0.01");
    expect(formatCurrencyUSDTitle(0.004)).toBe("$0.004");
    expect(formatCurrencyUSD(0.00012345)).toBe("< $0.01");
    expect(formatCurrencyUSDTitle(0.00012345)).toBe("$0.00012345");
    expect(formatCurrencyUSD(0.009999)).toBe("< $0.01");
  });

  it("0.01부터는 소수 2자리, title 없음", () => {
    expect(formatCurrencyUSD(0.01)).toBe("$0.01");
    expect(formatCurrencyUSDTitle(0.01)).toBeUndefined();
  });

  it("그 밖은 소수 2자리 반올림·천 단위 쉼표", () => {
    expect(formatCurrencyUSD(12.345)).toBe("$12.35");
    expect(formatCurrencyUSD(1234567.891)).toBe("$1,234,567.89");
    expect(formatCurrencyUSD(5)).toBe("$5.00");
    expect(formatCurrencyUSDTitle(12.345)).toBeUndefined();
  });

  it("null·NaN은 「—」, title 없음", () => {
    expect(formatCurrencyUSD(null)).toBe(EMPTY);
    expect(formatCurrencyUSD(undefined)).toBe(EMPTY);
    expect(formatCurrencyUSD(Number.NaN)).toBe(EMPTY);
    expect(formatCurrencyUSDTitle(null)).toBeUndefined();
  });
});

describe("formatDuration", () => {
  it("1초 미만은 「850ms」", () => {
    expect(formatDuration(0)).toBe("0ms");
    expect(formatDuration(850)).toBe("850ms");
    expect(formatDuration(12.345)).toBe("12ms");
  });

  it("1초 이상은 「17.9초」(소수 1자리)", () => {
    expect(formatDuration(1000)).toBe("1.0초");
    expect(formatDuration(17_900)).toBe("17.9초");
    expect(formatDuration(17_949)).toBe("17.9초");
    expect(formatDuration(1_234_500)).toBe("1,234.5초");
  });

  it("반올림하면 1000ms가 되는 값은 초 단위", () => {
    expect(formatDuration(999.6)).toBe("1.0초");
    expect(formatDuration(999.4)).toBe("999ms");
  });

  it("null·NaN은 「—」", () => {
    expect(formatDuration(null)).toBe(EMPTY);
    expect(formatDuration(Number.NaN)).toBe(EMPTY);
  });
});

describe("formatCompact — 그래프 축 축약 단위(이슈 #219 「50K·0.5M·1K」)", () => {
  it("값 크기에 맞는 단위를 고른다", () => {
    expect(formatCompact(50_000)).toBe("50K");
    expect(formatCompact(1000)).toBe("1K");
    expect(formatCompact(1_250_000)).toBe("1.25M");
    expect(formatCompact(3_000_000_000)).toBe("3B");
    expect(formatCompact(999)).toBe("999");
    expect(formatCompact(0)).toBe("0");
    expect(formatCompact(1234)).toBe("1.23K");
  });

  it("단위를 주면 그 단위로 맞춘다 — 축 눈금 「0, 0.5M, 1M」", () => {
    expect([0, 500_000, 1_000_000].map((v) => formatCompact(v, "M"))).toEqual(["0", "0.5M", "1M"]);
    expect(formatCompact(250_000, "M")).toBe("0.25M");
    expect(formatCompact(20_000, "K")).toBe("20K");
    expect(formatCompact(5, "")).toBe("5");
  });

  it("compactUnitFor — 1,000 미만은 단위 없음, 음수는 절댓값으로", () => {
    expect(compactUnitFor(999)).toBe("");
    expect(compactUnitFor(1000)).toBe("K");
    expect(compactUnitFor(1_000_000)).toBe("M");
    expect(compactUnitFor(-2_000)).toBe("K");
    expect(compactUnitFor(null)).toBe("");
  });

  it("null·NaN은 「—」", () => {
    expect(formatCompact(null)).toBe(EMPTY);
    expect(formatCompact(Number.NaN)).toBe(EMPTY);
  });
});

describe("formatCalendarDate · formatCalendarMonthDay — 서버 달력 날짜를 시간대 이동 없이", () => {
  it("「2026. 10. 27.」·「10. 27.」 — 브라우저 시간대와 무관하게 같은 날", () => {
    expect(formatCalendarDate("2026-10-27")).toBe("2026. 10. 27.");
    expect(formatCalendarDate("2026-01-01")).toBe("2026. 1. 1.");
    expect(formatCalendarMonthDay("2026-10-27")).toBe("10. 27.");
    expect(formatCalendarMonthDay("2026-09-03")).toBe("9. 3.");
  });

  it("해석할 수 없는 값은 「—」", () => {
    expect(formatCalendarDate(null)).toBe(EMPTY);
    expect(formatCalendarDate("2026-02-30")).toBe(EMPTY);
    expect(formatCalendarDate("2026-10-27T00:00:00Z")).toBe(EMPTY);
    expect(formatCalendarMonthDay(undefined)).toBe(EMPTY);
  });
});

describe("formatTimeZoneLabel — 참고 화면의 「UTC-8」 자리", () => {
  const JULY = new Date(Date.UTC(2026, 6, 1));
  const JANUARY = new Date(Date.UTC(2026, 0, 15));

  it("배포 표시 시간대(KST)는 약어로", () => {
    expect(formatTimeZoneLabel("Asia/Seoul")).toBe("Asia/Seoul (KST)");
    expect(formatTimeZoneLabel("Asia/Tokyo")).toBe("Asia/Tokyo (JST)");
  });

  it("UTC는 그대로, 그 밖은 그 시점의 UTC 오프셋", () => {
    expect(formatTimeZoneLabel("UTC")).toBe("UTC");
    expect(formatTimeZoneLabel("Etc/UTC")).toBe("UTC");
    expect(formatTimeZoneLabel("America/Los_Angeles", JANUARY)).toBe("America/Los_Angeles (UTC-8)");
    expect(formatTimeZoneLabel("America/Los_Angeles", JULY)).toBe("America/Los_Angeles (UTC-7)");
    expect(formatTimeZoneLabel("Asia/Kolkata", JULY)).toBe("Asia/Kolkata (UTC+5:30)");
  });

  it("해석할 수 없는 이름은 그대로, 비면 「—」", () => {
    expect(formatTimeZoneLabel("Not/AZone")).toBe("Not/AZone");
    expect(formatTimeZoneLabel("")).toBe(EMPTY);
    expect(formatTimeZoneLabel(null)).toBe(EMPTY);
  });
});
