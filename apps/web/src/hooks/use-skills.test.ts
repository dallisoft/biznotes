import { afterEach, describe, expect, it, vi } from "vitest";
import { encodeSkillFilePath, fetchSkillFile } from "@/hooks/use-skills";

afterEach(async () => {
  await new Promise((r) => setTimeout(r, 0));
  vi.unstubAllGlobals();
});

describe("encodeSkillFilePath", () => {
  it("preserves directory separators and encodes URL-reserved file-name characters", () => {
    expect(encodeSkillFilePath("assets/logo#2?final.json")).toBe(
      "assets/logo%232%3Ffinal.json",
    );
  });
});

describe("fetchSkillFile", () => {
  function respondWith(body: BodyInit, contentType: string) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(body, { headers: { "Content-Type": contentType } })),
    );
  }

  it("서버가 text/plain으로 내린 파일은 문자열로 읽는다(.sh 같은 스크립트 포함)", async () => {
    respondWith("#!/bin/sh\necho hi\n", "text/plain; charset=utf-8");
    await expect(fetchSkillFile("s1", "scripts/run.sh")).resolves.toBe("#!/bin/sh\necho hi\n");
  });

  it("바이너리는 null — 편집기가 텍스트로 열어 덮어쓰지 않는다", async () => {
    respondWith(new Uint8Array([0x89, 0x50, 0x4e, 0x47]), "image/png");
    await expect(fetchSkillFile("s1", "assets/logo.png")).resolves.toBeNull();
    respondWith(new Uint8Array([0xff, 0xfe, 0x00]), "application/octet-stream");
    await expect(fetchSkillFile("s1", "assets/data.bin")).resolves.toBeNull();
  });
});
