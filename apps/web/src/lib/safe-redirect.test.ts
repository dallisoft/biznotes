import { describe, expect, it } from "vitest";
import { CHANGE_PASSWORD_PATH, changePasswordHref, safeInternalPath } from "@/lib/safe-redirect";

const ORIGIN = "http://localhost:3000";

describe("safeInternalPath (AC-3.3, D-34′ — 로그인·변경 화면 공용)", () => {
  it.each([
    ["/", "/"],
    ["/notebooks/1", "/notebooks/1"],
    ["/notebooks/1?s=2&x=%2F", "/notebooks/1?s=2&x=%2F"],
    ["/workflows#top", "/workflows#top"],
    ["/notebooks/1?s=2#m", "/notebooks/1?s=2#m"],
  ])("내부 경로·query·hash는 그대로 통과한다: %s", (raw, expected) => {
    expect(safeInternalPath(raw, ORIGIN)).toBe(expected);
  });

  it.each([
    ["//attacker.example/p"],
    ["/\\attacker.example"],
    ["/a\\b"],
    ["https://attacker.example/p"],
    ["https:"],
    ["javascript:alert(1)"],
    ["notebooks/1"],
    [""],
    ["/a\u0000b"],
    ["/a\nb"],
    ["/a\tb"],
    ["/a\u001Fb"],
    ["/a\u007Fb"],
  ])("외부·scheme·역슬래시·제어 문자는 거부한다: %j", (raw) => {
    expect(safeInternalPath(raw, ORIGIN)).toBe("/");
  });

  it.each([
    ["/a/..//attacker.example/p"],
    ["/%2e%2e//attacker.example/p"],
    ["/.//attacker.example"],
    ["/a/%2e%2e/%2e%2e//attacker.example"],
  ])("정규화 뒤 scheme-relative가 되는 경로는 반환값 재검사로 거부한다: %s", (raw) => {
    expect(safeInternalPath(raw, ORIGIN)).toBe("/");
  });

  it("퍼센트 인코딩 역슬래시는 디코드되지 않아 같은 origin 경로로 남는다(재해석 불변식)", () => {
    expect(safeInternalPath("/%5Cx", ORIGIN)).toBe("/%5Cx");
    expect(new URL(safeInternalPath("/%5Cx", ORIGIN), ORIGIN).origin).toBe(ORIGIN);
  });

  it("null·undefined는 /", () => {
    expect(safeInternalPath(null, ORIGIN)).toBe("/");
    expect(safeInternalPath(undefined, ORIGIN)).toBe("/");
  });

  it("origin을 생략하면 현재 문서 origin을 쓴다", () => {
    expect(safeInternalPath("/x?y=1")).toBe("/x?y=1");
    expect(safeInternalPath("//x")).toBe("/");
  });
});

describe("changePasswordHref", () => {
  it("돌아갈 곳을 인코딩해 redirect로 싣는다", () => {
    expect(changePasswordHref("/notebooks/1?s=2")).toBe(
      `${CHANGE_PASSWORD_PATH}?redirect=${encodeURIComponent("/notebooks/1?s=2")}`,
    );
  });

  it("/·외부 경로·변경 화면 자신이면 쿼리를 붙이지 않는다", () => {
    expect(changePasswordHref("/")).toBe(CHANGE_PASSWORD_PATH);
    expect(changePasswordHref("//attacker.example")).toBe(CHANGE_PASSWORD_PATH);
    expect(changePasswordHref(null)).toBe(CHANGE_PASSWORD_PATH);
    expect(changePasswordHref("/change-password?redirect=%2Fx")).toBe(CHANGE_PASSWORD_PATH);
  });
});
