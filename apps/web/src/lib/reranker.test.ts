import { describe, expect, it } from "vitest";
import { normalizeRerankKey, PYTHON_WHITESPACE_CODE_POINTS, pythonStrip } from "./reranker";

describe("normalizeRerankKey", () => {
  it("접두사 없는 옛 값은 fastembed: 키로 본다", () => {
    expect(normalizeRerankKey("BAAI/bge-reranker-base")).toBe("fastembed:BAAI/bge-reranker-base");
    expect(normalizeRerankKey("  BAAI/bge-reranker-base ")).toBe("fastembed:BAAI/bge-reranker-base");
  });

  it("접두사가 있는 값은 그대로 둔다", () => {
    const gateway = "gateway:3f0c9f8e-1111-4222-8333-944445555666:openai/rr";
    expect(normalizeRerankKey(gateway)).toBe(gateway);
    expect(normalizeRerankKey("fastembed:BAAI/bge-reranker-base")).toBe(
      "fastembed:BAAI/bge-reranker-base",
    );
  });

  it("빈 값(자동)은 빈 문자열", () => {
    expect(normalizeRerankKey("")).toBe("");
    expect(normalizeRerankKey("   ")).toBe("");
    expect(normalizeRerankKey(undefined)).toBe("");
    expect(normalizeRerankKey(null)).toBe("");
  });
});

describe("normalizeRerankKey — 코어 Python str.strip()과 같은 공백(리뷰 P2-07)", () => {
  it("Python 공백 29자는 양끝에서 걷어낸다(JS trim이 남기는 U+0085·U+001C~001F 포함)", () => {
    for (const code of PYTHON_WHITESPACE_CODE_POINTS) {
      const w = String.fromCharCode(code);
      expect(normalizeRerankKey(`${w}BAAI/bge-reranker-base${w}`)).toBe(
        "fastembed:BAAI/bge-reranker-base",
      );
      expect(normalizeRerankKey(w)).toBe("");
    }
    expect(PYTHON_WHITESPACE_CODE_POINTS).toHaveLength(29);
  });

  it("Python 공백이 아닌 U+FEFF·U+200B는 남긴다(코어도 남긴다)", () => {
    expect(normalizeRerankKey("﻿BAAI/bge-reranker-base﻿")).toBe(
      "fastembed:﻿BAAI/bge-reranker-base﻿",
    );
    expect(normalizeRerankKey("​x")).toBe("fastembed:​x");
  });

  it("가운데 공백은 건드리지 않는다", () => {
    expect(pythonStrip("\u0085a\u0085b\u0085")).toBe("a\u0085b");
  });
});
