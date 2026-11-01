import { describe, expect, it, vi } from "vitest";
import {
  canonicalFileIdentity,
  canonicalizeFilename,
  filterNewFiles,
  splitOversizedFiles,
  uploadFilesWithConcurrency,
} from "@/lib/file-upload";

function file(name: string, size = 1): File {
  return { name, size } as File;
}

describe("file upload identity and filtering", () => {
  it("uses basename, NFC, trim, case-sensitive name, and byte size", () => {
    expect(canonicalizeFilename("/tmp/  cafe\u0301.txt  ")).toBe("café.txt");
    expect(canonicalFileIdentity(file("folder\\Cafe.txt", 7))).toBe("Cafe.txt\u00007");
    expect(canonicalFileIdentity(file("Cafe.txt", 8))).not.toBe(
      canonicalFileIdentity(file("cafe.txt", 8)),
    );
  });

  it("filters existing file sources and duplicates from one selection", () => {
    const result = filterNewFiles(
      [
        file("/picked/cafe\u0301.txt", 3),
        file("A.txt", 4),
        file("/other/A.txt", 4),
        file("a.txt", 4),
        file("same-name.txt", 3),
      ],
      [
        { type: "file", title: "stored/café.txt", size_bytes: 3 },
        { type: "web", title: "same-name.txt", size_bytes: 3 },
      ],
    );

    expect(result.files.map((item) => item.name)).toEqual([
      "A.txt",
      "a.txt",
      "same-name.txt",
    ]);
    expect(result.counts).toMatchObject({
      existingClientSkipped: 1,
      sameBatchSkipped: 1,
    });
  });
});

describe("splitOversizedFiles — 업로드 최대 크기 사전 검사(코어 v0.26.0 #388)", () => {
  it("한도를 넘는 파일만 빼고 입력 순서를 지킨다(한도와 같은 크기는 보낸다)", () => {
    const result = splitOversizedFiles(
      [file("a.pdf", 10), file("big.pdf", 11), file("b.pdf", 1), file("huge.pdf", 99)],
      10,
    );
    expect(result.files.map((item) => item.name)).toEqual(["a.pdf", "b.pdf"]);
    expect(result.oversized.map((item) => item.name)).toEqual(["big.pdf", "huge.pdf"]);
  });
});

describe("uploadFilesWithConcurrency", () => {
  it("caps in-flight uploads at four, preserves successes, and classifies once", async () => {
    const files = [
      file("one.txt"),
      file("duplicate.txt"),
      file("invalid.txt"),
      file("too-large.pdf"),
      file("limited.txt"),
      file("network.txt"),
      file("slow-success.txt"),
    ];
    let active = 0;
    let maxActive = 0;
    const upload = vi.fn(async (item: File) => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      await new Promise((resolve) => setTimeout(resolve, item.name === "one.txt" ? 10 : 1));
      active -= 1;
      if (item.name === "duplicate.txt") throw { status: 409 };
      if (item.name === "invalid.txt") throw { status: 422 };
      if (item.name === "too-large.pdf") throw { status: 413 };
      if (item.name === "limited.txt") throw { status: 429 };
      if (item.name === "network.txt") throw new TypeError("Failed to fetch");
      return item.name;
    });

    const result = await uploadFilesWithConcurrency(files, upload, {
      initialCounts: { existingClientSkipped: 2, sameBatchSkipped: 1 },
    });

    expect(maxActive).toBeLessThanOrEqual(4);
    expect(upload).toHaveBeenCalledTimes(files.length);
    expect(result.uploaded).toEqual(["one.txt", "slow-success.txt"]);
    expect(result.counts).toEqual({
      existingClientSkipped: 2,
      sameBatchSkipped: 1,
      oversizedClientSkipped: 0,
      serverDuplicate409: 1,
      uploaded: 2,
      validationOrCapacity422: 1,
      // 서버(코어 관문·프록시)의 413은 사전 검사를 지난 뒤의 최종 판정 — 네트워크 실패로 세지 않는다
      tooLarge413: 1,
      rateLimited429: 1,
      networkFailed: 1,
    });
  });

  it("does not retry a failed request", async () => {
    const upload = vi.fn().mockRejectedValue({ status: 422 });
    const result = await uploadFilesWithConcurrency([file("bad.txt")], upload);

    expect(upload).toHaveBeenCalledTimes(1);
    expect(result.counts.validationOrCapacity422).toBe(1);
  });
});
