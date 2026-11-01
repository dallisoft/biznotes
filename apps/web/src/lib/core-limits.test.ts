import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api-client";
import {
  bytesToMegabytes,
  effectiveLimitText,
  formatMegabytes,
  formatUploadSize,
  INVALID_LIMIT_MESSAGE,
  limitValidationMessages,
  megabytesToBytes,
  MIB,
  RATE_LIMIT_LABEL,
  rateLimitLabel,
  settleEdits,
  sourceLimitFrom,
} from "@/lib/core-limits";

const FIELDS = { rate_limit_chat: { label: "채팅 질문" }, max_upload_bytes: { label: "업로드", format: formatMegabytes } };

describe("limitValidationMessages — 저장 422 문구", () => {
  it("422가 아니면 null(토스트 경로)", () => {
    expect(limitValidationMessages(new ApiError(500, "boom"), FIELDS)).toBeNull();
    expect(limitValidationMessages(new Error("x"), FIELDS)).toBeNull();
    expect(limitValidationMessages(null, FIELDS)).toBeNull();
  });

  it("범위 위반은 서버 경계(ctx)로, 필드 형식(MB)을 따라 보인다", () => {
    const error = new ApiError(422, "[]", {
      detail: [
        { type: "greater_than_equal", loc: ["body", "rate_limit_chat"], msg: "…", ctx: { ge: 1 } },
        { type: "less_than_equal", loc: ["body", "max_upload_bytes"], msg: "…", ctx: { le: 500 * MIB } },
        { type: "int_parsing", loc: ["body", "rate_limit_chat"], msg: "…" },
        { type: "extra_forbidden", loc: ["body", "rate_limit_x"], msg: "…" },
      ],
    });
    expect(limitValidationMessages(error, FIELDS)).toEqual([
      "채팅 질문: 1 이상이어야 합니다",
      "업로드: 500MB 이하여야 합니다",
      "채팅 질문: 정수여야 합니다",
      "rate_limit_x: 서버가 모르는 항목입니다",
    ]);
  });

  it("모르는 검증 유형은 서버 msg를 붙이고, 입력값(input)은 쓰지 않는다", () => {
    const error = new ApiError(422, "[]", {
      detail: [{ type: "custom", loc: ["body", "rate_limit_chat"], msg: "too spicy", input: "secret-ish-77" }],
    });
    const messages = limitValidationMessages(error, FIELDS);
    expect(messages).toEqual(["채팅 질문: too spicy"]);
    expect(messages?.join()).not.toContain("secret-ish-77");
  });

  it("문자열 detail은 그대로, 모양을 모르는 본문은 고정 문구", () => {
    expect(limitValidationMessages(new ApiError(422, "x", { detail: "범위 밖" }), FIELDS)).toEqual(["범위 밖"]);
    expect(limitValidationMessages(new ApiError(422, "x", {}), FIELDS)).toEqual([INVALID_LIMIT_MESSAGE]);
    expect(limitValidationMessages(new ApiError(422, "x", { detail: [null] }), FIELDS)).toEqual([INVALID_LIMIT_MESSAGE]);
  });
});

describe("settleEdits — 저장 뒤 남길 편집", () => {
  it("보낸 값 그대로인 칸만 지우고, 저장 중에 다시 고친 칸·보내지 않은 칸은 남긴다", () => {
    expect(settleEdits({ a: "1", b: "22", c: "3" }, { a: "1", b: "2" })).toEqual({ b: "22", c: "3" });
    expect(settleEdits({ a: "" }, { a: "" })).toEqual({});
  });
});

describe("MB 변환 — 1MB = 1,048,576바이트(코어 50 MiB 기본값과 같은 단위)", () => {
  it("바이트 ↔ MB — 반올림하지 않아 같은 바이트로 되돌아온다", () => {
    expect(bytesToMegabytes(52_428_800)).toBe(50);
    expect(bytesToMegabytes(MIB + MIB / 2)).toBe(1.5);
    for (const bytes of [1, 1000, 524_288, 52_428_801]) {
      expect(megabytesToBytes(bytesToMegabytes(bytes))).toBe(bytes);
    }
    expect(megabytesToBytes(50)).toBe(52_428_800);
    expect(megabytesToBytes(0.5)).toBe(524_288);
    expect(megabytesToBytes(0.000001)).toBe(1);
  });

  it("범위를 주면 반올림한 뒤 그 안으로 맞춘다(코어 업로드 재정의 1바이트~하드 상한)", () => {
    const bounds = { min: 1, max: 500 * MIB };
    expect(megabytesToBytes(0.0000001, bounds)).toBe(1);
    expect(megabytesToBytes(500.0000004, bounds)).toBe(500 * MIB);
    expect(megabytesToBytes(0.5, bounds)).toBe(524_288);
  });

  it("MB 표시 — 1MB 미만도 0으로 뭉개지지 않고, 관리 화면은 나누어떨어지지 않으면 바이트를 함께 보인다", () => {
    expect(formatMegabytes(52_428_800)).toBe("50MB");
    expect(formatMegabytes(524_288_000)).toBe("500MB");
    expect(formatMegabytes(524_288)).toBe("0.5MB");
    expect(formatMegabytes(1)).toBe("0.000001MB");
    expect(formatMegabytes(undefined)).toBe("—");
    expect(formatUploadSize(52_428_800)).toBe("50MB");
    expect(formatUploadSize(524_288)).toBe("0.5MB (524,288바이트)");
    expect(formatUploadSize(1000)).toBe("0.000954MB (1,000바이트)");
    expect(formatUploadSize(52_428_801)).toBe("50MB (52,428,801바이트)");
  });
});

describe("라벨", () => {
  it("레이트 리밋 7종은 라벨이 서로 다르고, 모르는 키는 원시 키", () => {
    const labels = Object.values(RATE_LIMIT_LABEL).map((v) => v.label);
    expect(labels).toHaveLength(7);
    expect(new Set(labels).size).toBe(7);
    expect(rateLimitLabel("rate_limit_chat")).toBe("채팅 질문");
    expect(rateLimitLabel("rate_limit_future")).toBe("rate_limit_future");
  });

  it("적용 한도 한 줄 — 값·출처, 무제한", () => {
    expect(effectiveLimitText({ value: 1000, source: "user" })).toBe("1,000 · 사용자 지정");
    expect(effectiveLimitText({ value: 0, source: "default" })).toBe("0 · 기본값");
    expect(effectiveLimitText({ value: null, source: "unlimited" })).toBe("무제한");
  });
});

describe("sourceLimitFrom — 노트북당 소스 상한(코어 #445, #281)", () => {
  it("1 이상 정수만 받고 그 밖은 undefined다", () => {
    expect(sourceLimitFrom({ max_sources_per_notebook: 500 })).toBe(500);
    expect(sourceLimitFrom({ max_sources_per_notebook: 0 })).toBeUndefined();
    expect(sourceLimitFrom({ max_sources_per_notebook: 1.5 })).toBeUndefined();
    expect(sourceLimitFrom({ max_sources_per_notebook: null })).toBeUndefined();
    expect(sourceLimitFrom({})).toBeUndefined();
    expect(sourceLimitFrom(undefined)).toBeUndefined();
  });
});
