import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api-client";
import { LOGIN_ERROR_MESSAGE, loginErrorMessage } from "@/lib/login-error";
import { NETWORK_ERROR_MESSAGE } from "@/lib/notify";

describe("loginErrorMessage — 상태별 고정 문구(#93)", () => {
  it.each([
    [401, LOGIN_ERROR_MESSAGE.invalidCredentials],
    [403, LOGIN_ERROR_MESSAGE.disabled],
    [422, LOGIN_ERROR_MESSAGE.invalidInput],
    [429, LOGIN_ERROR_MESSAGE.rateLimited],
    [500, LOGIN_ERROR_MESSAGE.failed],
  ])("%i → 사전 문구", (status, message) => {
    expect(loginErrorMessage(new ApiError(status, "server says"))).toBe(message);
  });

  it("422 본문에 입력값이 있어도 문구에 넣지 않는다", () => {
    const body = { detail: [{ loc: ["body", "password"], msg: "too long", input: "s3cret-pw" }] };
    const message = loginErrorMessage(new ApiError(422, "too long", body));
    expect(message).toBe(LOGIN_ERROR_MESSAGE.invalidInput);
    expect(message).not.toContain("s3cret-pw");
  });

  it("네트워크 오류(TypeError)는 연결 안내, 그 밖은 일반 문구", () => {
    expect(loginErrorMessage(new TypeError("Failed to fetch"))).toBe(NETWORK_ERROR_MESSAGE);
    expect(loginErrorMessage(new Error("boom"))).toBe(LOGIN_ERROR_MESSAGE.failed);
  });
});
