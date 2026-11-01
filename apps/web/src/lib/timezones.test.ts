import { describe, expect, it } from "vitest";
import {
  DISPLAY_TIMEZONE,
  displayDayEnd,
  displayDayStart,
  KST,
} from "./timezones";

// #213(코어 v0.24.0 #351·#352) — 표시 시간대 상수와 하루 경계.

describe("표시 시간대", () => {
  it("배포 표시 시간대는 Asia/Seoul이다(API·워커 `DISPLAY_TIMEZONE`과 같은 값)", () => {
    expect(KST).toBe("Asia/Seoul");
    expect(DISPLAY_TIMEZONE).toBe(KST);
  });

  it("YYYY-MM-DD를 표시 시간대의 하루 경계(+09:00)로 바꾼다 — 브라우저 시간대와 무관한 문자열 조립", () => {
    expect(displayDayStart("2026-10-01")).toBe("2026-10-01T00:00:00.000+09:00");
    expect(displayDayEnd("2026-10-10")).toBe("2026-10-10T23:59:59.999+09:00");
    // UTC로는 전날 15:00 ~ 그날 14:59:59.999다
    expect(new Date(displayDayStart("2026-10-01")).toISOString()).toBe("2026-09-30T15:00:00.000Z");
    expect(new Date(displayDayEnd("2026-10-10")).toISOString()).toBe("2026-10-10T14:59:59.999Z");
  });
});
