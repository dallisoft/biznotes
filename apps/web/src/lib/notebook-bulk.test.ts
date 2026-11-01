import { describe, expect, it } from "vitest";
import { ApiError, PASSWORD_CHANGE_REQUIRED } from "@/lib/api-client";
import {
  alreadySharedMessage,
  BULK_MESSAGES,
  type BulkItemResult,
  classifyError,
  decideShare,
  formatDeleteSummary,
  formatShareSummary,
  previewTitles,
  type SharePermission,
  settledIds,
  STOP_NOTICES,
  summarizeResults,
} from "./notebook-bulk";

// plan §5.4 오류 분류표 — 실제 classifyError를 실행한다.

describe("classifyError", () => {
  it.each([
    [new ApiError(403, "Only the owner can delete"), "forbidden", BULK_MESSAGES.forbidden],
    [new ApiError(404, "Notebook not found"), "not_found", BULK_MESSAGES.notFound],
    [new ApiError(422, "validation"), "invalid", BULK_MESSAGES.invalid],
    [new ApiError(500, "Internal Server Error"), "server", BULK_MESSAGES.server],
    [new ApiError(503, "Service Unavailable"), "server", BULK_MESSAGES.server],
    [new ApiError(409, "conflict"), "other", "요청 실패 (HTTP 409)"],
  ])("%s → 항목 실패(%s), 전역 중단 아님", (error, kind, message) => {
    expect(classifyError(error)).toEqual({
      kind,
      message,
      globalStop: false,
      targetStop: false,
      unconfirmed: false,
    });
  });

  it("401(refresh 실패 뒤)과 403 password_change_required는 전역 중단이다", () => {
    expect(classifyError(new ApiError(401, "Not authenticated"))).toMatchObject({
      kind: "auth_expired",
      message: BULK_MESSAGES.authExpired,
      globalStop: true,
    });
    const pcr = new ApiError(403, "{}", { detail: { code: PASSWORD_CHANGE_REQUIRED } }, PASSWORD_CHANGE_REQUIRED);
    expect(classifyError(pcr)).toMatchObject({
      kind: "password_change_required",
      message: BULK_MESSAGES.passwordChangeRequired,
      globalStop: true,
    });
  });

  it("응답 손실(ApiError가 아닌 오류)은 실패 + 확인 불가다 — 성공으로 세지 않는다", () => {
    for (const error of [new TypeError("Failed to fetch"), new Error("socket hang up"), "x"]) {
      expect(classifyError(error)).toEqual({
        kind: "unconfirmed",
        message: BULK_MESSAGES.unconfirmed,
        globalStop: false,
        targetStop: false,
        unconfirmed: true,
      });
    }
  });

  it("그룹 대상 404 문구(코어 v0.20.0 \"Target group not found\", D-08)는 대상 중단으로 분류하고 서버 detail 원문을 보이지 않는다", () => {
    expect(classifyError(new ApiError(404, "Target group not found"))).toEqual({
      kind: "target_group_missing",
      message: BULK_MESSAGES.targetGroupMissing,
      globalStop: false,
      targetStop: true,
      unconfirmed: false,
    });
    expect(BULK_MESSAGES.targetGroupMissing).not.toContain("Target group not found");
  });

  it("사용자 대상 404 문구(옛 이메일 공유)는 더 이상 대상 중단이 아니다 — 웹이 사용자 대상을 보내지 않는다(core-v0.20.0 AC-3.4)", () => {
    for (const detail of ["No user with that email", "Target user not found"]) {
      expect(classifyError(new ApiError(404, detail))).toMatchObject({ kind: "not_found", targetStop: false });
    }
    expect(Object.keys(BULK_MESSAGES)).not.toContain("targetEmailMissing");
    expect(Object.keys(BULK_MESSAGES)).not.toContain("targetUserMissing");
  });
});

function result(id: string, status: BulkItemResult["status"], unconfirmed = false): BulkItemResult {
  return { id, title: id, status, message: "", unconfirmed };
}

describe("결과 요약·선택 해제 대상", () => {
  const results = [
    result("a", "succeeded"),
    result("b", "failed"),
    result("c", "failed", true),
    result("d", "not_run"),
    result("e", "succeeded"),
    result("f", "skipped"),
  ];

  it("요약 수는 서로 겹치지 않고 합이 전체와 같다", () => {
    const s = summarizeResults(results);
    expect(s).toEqual({ total: 6, succeeded: 2, skipped: 1, failed: 1, unconfirmed: 1, notRun: 1 });
    expect(s.succeeded + s.skipped + s.failed + s.unconfirmed + s.notRun).toBe(s.total);
    expect(formatDeleteSummary(s)).toBe("삭제 2 · 실패 1 · 확인 불가 1 · 미실행 1");
  });

  it("S-7 — 성공·건너뜀만 선택에서 뺀다(실패·확인 불가·미실행은 유지)", () => {
    expect(settledIds(results)).toEqual(["a", "e", "f"]);
  });
});

describe("previewTitles (D-12)", () => {
  const targets = (n: number) => Array.from({ length: n }, (_, i) => ({ id: `${i}`, title: `T${i}` }));

  it("10개 이하면 모두 보이고 나머지 0", () => {
    expect(previewTitles(targets(10))).toEqual({
      shown: ["T0", "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"],
      rest: 0,
    });
  });

  it("11개 이상이면 앞 10개와 「외 k개」의 k", () => {
    const { shown, rest } = previewTitles(targets(13));
    expect(shown).toHaveLength(10);
    expect(shown[9]).toBe("T9");
    expect(rest).toBe(3);
  });
});

// ---- 일괄 공유(Phase 3, core-v0.20.0 §5.8 그룹판) — 결정표·요약·중단 안내 ----

describe("decideShare (§5.5 결정표 그룹판, AC-3.2·core-v0.20.0 AC-3.3)", () => {
  const G = "group-g";
  const row = (permission: "read" | "write", group_id: string = G) => ({ group_id, permission });

  // read/write × 기존 {∅, read, write, read+write} 8조합 — 기대값은 결정표 3행 그대로
  it.each<[SharePermission, ReturnType<typeof row>[], ReturnType<typeof decideShare>]>([
    ["read", [], { action: "post", result: "shared" }],
    ["read", [row("read")], { action: "skip", existing: "read" }],
    ["read", [row("write")], { action: "skip", existing: "write" }],
    ["read", [row("read"), row("write")], { action: "skip", existing: "write" }],
    ["write", [], { action: "post", result: "shared" }],
    ["write", [row("read")], { action: "post", result: "added_write" }],
    ["write", [row("write")], { action: "skip", existing: "write" }],
    ["write", [row("write"), row("read")], { action: "skip", existing: "write" }],
  ])("요청 %s, 기존 %j → %j", (p, rows, expected) => {
    expect(decideShare(G, p, rows)).toEqual(expected);
  });

  it("하향 POST가 나오는 조합은 0개다 — POST는 기존 최대 권한보다 높을 때만 나온다", () => {
    const rank = { read: 1, write: 2 } as const;
    const existingSets: ("read" | "write")[][] = [[], ["read"], ["write"], ["read", "write"]];
    for (const p of ["read", "write"] as const) {
      for (const set of existingSets) {
        const d = decideShare(G, p, set.map((perm) => row(perm)));
        if (d.action === "post" && set.length > 0) {
          expect(Math.max(...set.map((perm) => rank[perm]))).toBeLessThan(rank[p]);
        }
      }
    }
  });

  it("다른 그룹 행은 보지 않는다", () => {
    expect(decideShare(G, "read", [row("write", "other-group")])).toEqual({
      action: "post",
      result: "shared",
    });
  });

  it("형식이 어긋난 행(null·알 수 없는 권한)은 기존 권한으로 세지 않는다", () => {
    const malformed = [null, { group_id: G, permission: "admin" }] as unknown as Parameters<typeof decideShare>[2];
    expect(decideShare(G, "read", malformed)).toEqual({ action: "post", result: "shared" });
    expect(decideShare(G, "write", [...malformed, row("read")])).toEqual({
      action: "post",
      result: "added_write",
    });
  });

  it("건너뜀 문구는 기존 최대 권한의 라벨을 쓴다(원시값 read/write 아님 — §6.3.2)", () => {
    expect(alreadySharedMessage("편집 가능")).toBe("이미 편집 가능 권한으로 공유됨");
    expect(alreadySharedMessage("읽기 전용")).toBe("이미 읽기 전용 권한으로 공유됨");
    // 코어 v0.19.0+는 같은 대상 POST가 기존 read 행을 write로 올린다 — 행을 더하지 않는다(core-v0.20.0 AC-1.9 ①)
    expect(BULK_MESSAGES.addedWrite).toBe("편집 가능 권한으로 변경됨");
  });
});

describe("공유 요약·중단 안내", () => {
  it("요약은 「공유 · 건너뜀 · 실패 · 확인 불가 · 미실행」 다섯 수다", () => {
    const s = summarizeResults([
      result("a", "succeeded"),
      result("b", "skipped"),
      result("c", "failed"),
      result("d", "failed", true),
      result("e", "not_run"),
      result("f", "succeeded"),
    ]);
    expect(formatShareSummary(s)).toBe("공유 2 · 건너뜀 1 · 실패 1 · 확인 불가 1 · 미실행 1");
  });

  it("중단 이유는 사용자·인증 2종·그룹 대상 넷뿐이고(probe·사용자 대상 중단 없음), 그룹 대상 중단 안내가 있다", () => {
    expect(Object.keys(STOP_NOTICES).sort()).toEqual(
      ["auth_expired", "password_change_required", "target_group_missing", "user"].sort(),
    );
    expect(STOP_NOTICES.target_group_missing).toBe(
      "이 그룹에는 공유할 수 없어 중단했습니다 — 소유자가 속한 그룹인지 확인한 뒤 남은 항목을 다시 실행하세요",
    );
  });

  it("그룹 0개 안내는 단건·일괄이 같은 문구이고, probe·보상 DELETE 문구는 없다", () => {
    expect(BULK_MESSAGES.noGroups).toBe("공유할 그룹이 없습니다 — 관리자에게 그룹을 요청하세요");
    expect("probeUnavailable" in BULK_MESSAGES).toBe(false);
    expect("duplicateMayRemain" in BULK_MESSAGES).toBe(false);
    expect(Object.values(BULK_MESSAGES).some((m) => m.includes("중복 공유 행"))).toBe(false);
  });
});
