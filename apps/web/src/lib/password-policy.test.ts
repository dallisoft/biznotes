import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api-client";
import {
  CHANGE_FAILED_MESSAGE,
  checkPasswordChange,
  describePasswordChangeError,
  INVALID_CURRENT_PASSWORD_MESSAGE,
  INVALID_INPUT_MESSAGE,
  INVALID_NEW_PASSWORD_MESSAGE,
  invalidNewPasswordReason,
  newPasswordErrorMessage,
  PASSWORD_ISSUE_MESSAGE,
  passwordLength,
  RATE_LIMITED_MESSAGE,
  utf8ByteLength,
} from "@/lib/password-policy";

const apiErr = (status: number, detail: unknown) => {
  const code =
    typeof detail === "object" && detail !== null && "code" in detail
      ? String((detail as { code: unknown }).code)
      : undefined;
  return new ApiError(status, "서버 메시지", { detail }, code);
};

describe("길이 계산", () => {
  it("글자 수는 코드 포인트로 센다(코어 len과 같음)", () => {
    expect(passwordLength("abcdefgh")).toBe(8);
    expect(passwordLength("😀😀😀😀")).toBe(4); // UTF-16 길이는 8
    expect(passwordLength("가나다라마바사아")).toBe(8);
  });

  it("바이트 수는 UTF-8(TextEncoder)로 센다", () => {
    expect(utf8ByteLength("a".repeat(72))).toBe(72);
    expect(utf8ByteLength("가")).toBe(3);
    expect(utf8ByteLength("😀")).toBe(4);
  });
});

describe("checkPasswordChange — 클라이언트 사전 검사 (AC-3.3)", () => {
  const ok = { current: "old-password", next: "new-password-1", confirm: "new-password-1" };

  it("규칙을 모두 지키면 null", () => {
    expect(checkPasswordChange(ok)).toBeNull();
    expect(checkPasswordChange({ ...ok, next: "a".repeat(72), confirm: "a".repeat(72) })).toBeNull();
  });

  it.each([
    [{ ...ok, current: "" }, "current_required"],
    [{ ...ok, next: "short7!", confirm: "short7!" }, "too_short"],
    [{ ...ok, next: "😀😀😀😀", confirm: "😀😀😀😀" }, "too_short"],
    [{ ...ok, next: "a".repeat(73), confirm: "a".repeat(73) }, "too_long_bytes"],
    [{ ...ok, next: "가".repeat(25), confirm: "가".repeat(25) }, "too_long_bytes"], // 75바이트
    [{ ...ok, confirm: "different-1" }, "mismatch"],
    [{ current: "same-password", next: "same-password", confirm: "same-password" }, "same_as_current"],
  ])("%j → %s", (input, issue) => {
    expect(checkPasswordChange(input)).toBe(issue);
  });

  it("문구에 입력값이 들어가지 않는다(T-17)", () => {
    for (const message of Object.values(PASSWORD_ISSUE_MESSAGE)) {
      expect(message).not.toContain("old-password");
      expect(message).not.toContain("new-password-1");
    }
  });
});

describe("describePasswordChangeError — 서버 오류 매핑 (AC-3.3)", () => {
  it("400 invalid_current_password → 현재 비밀번호 문구", () => {
    const err = apiErr(400, { code: "invalid_current_password", detail: "x" });
    expect(describePasswordChangeError(err)).toEqual({
      kind: "message",
      message: INVALID_CURRENT_PASSWORD_MESSAGE,
    });
  });

  it.each([
    ["too_short", PASSWORD_ISSUE_MESSAGE.too_short],
    ["too_long_bytes", PASSWORD_ISSUE_MESSAGE.too_long_bytes],
    ["same_as_current", PASSWORD_ISSUE_MESSAGE.same_as_current],
    ["unknown_reason", INVALID_NEW_PASSWORD_MESSAGE],
  ])("422 invalid_new_password reason=%s → 사전 문구", (reason, message) => {
    const err = apiErr(422, { code: "invalid_new_password", reason, detail: "서버 문구" });
    expect(describePasswordChangeError(err)).toEqual({ kind: "message", message });
  });

  it("code 없는 422(검증 본문)는 입력값을 보이지 않는 일반 문구", () => {
    const err = apiErr(422, [{ loc: ["body", "new_password"], input: "secret-input-value" }]);
    const out = describePasswordChangeError(err);
    expect(out).toEqual({ kind: "message", message: INVALID_INPUT_MESSAGE });
    expect(JSON.stringify(out)).not.toContain("secret-input-value");
  });

  it("429 → 레이트 리밋 문구", () => {
    expect(describePasswordChangeError(apiErr(429, "Too Many Requests"))).toEqual({
      kind: "message",
      message: RATE_LIMITED_MESSAGE,
    });
  });

  it("401 → 로그인, 404·405 → 미지원", () => {
    expect(describePasswordChangeError(apiErr(401, "x"))).toEqual({ kind: "login" });
    expect(describePasswordChangeError(apiErr(404, "Not Found"))).toEqual({ kind: "unsupported" });
    expect(describePasswordChangeError(apiErr(405, "Method Not Allowed"))).toEqual({
      kind: "unsupported",
    });
  });

  it("그 밖(500·네트워크 오류·다른 400)은 일반 실패 문구이고 서버 메시지를 쓰지 않는다", () => {
    for (const err of [apiErr(500, "boom"), new TypeError("Failed to fetch"), apiErr(400, "x")]) {
      expect(describePasswordChangeError(err)).toEqual({
        kind: "message",
        message: CHANGE_FAILED_MESSAGE,
      });
    }
  });
});

describe("newPasswordErrorMessage·invalidNewPasswordReason (관리자 생성 422 공용)", () => {
  it("reason을 읽고 문구로 바꾼다", () => {
    const err = apiErr(422, { code: "invalid_new_password", reason: "too_short", detail: "x" });
    expect(invalidNewPasswordReason(err)).toBe("too_short");
    expect(newPasswordErrorMessage(err)).toBe(PASSWORD_ISSUE_MESSAGE.too_short);
  });

  it("다른 오류에는 reason이 없다", () => {
    expect(invalidNewPasswordReason(apiErr(400, { code: "invalid_new_password", reason: "x" }))).toBeUndefined();
    expect(invalidNewPasswordReason(new Error("x"))).toBeUndefined();
    expect(invalidNewPasswordReason(apiErr(422, { code: "invalid_new_password" }))).toBeUndefined();
    expect(newPasswordErrorMessage(apiErr(422, "문자열 detail"))).toBe(INVALID_INPUT_MESSAGE);
  });
});
