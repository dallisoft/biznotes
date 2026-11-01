import { describe, expect, it } from "vitest";
import { isTextyArtifact, resolveArtifactViewer } from "@/lib/artifact-viewers";

describe("resolveArtifactViewer (AC-1.8)", () => {
  it.each([
    ["mindmap.json", "application/json", "mindmap"],
    ["flashcards.json", "application/json", "flashcards"],
    ["quiz.json", "application/json; charset=utf-8", "quiz"],
    ["infographic.json", "application/json", "infographic"],
    ["slide-deck.json", "application/json", "slide-deck"],
    ["slide-outline.json", "application/json", "slide-outline"],
    ["skill-package.json", "application/json", "json"],
    ["other.json", "application/json", "json"],
    ["mindmap.json", "text/plain", "text"],
    ["report.md", "text/markdown", "markdown"],
    ["notes.md", "text/plain", "markdown"],
    ["audio.mp3", "audio/mpeg", "audio"],
    ["slide-image-2.png", "image/png", "image"],
    ["art.jpg", "image/jpeg", "image"],
    ["a.txt", "text/plain", "text"],
  ])("%s (%s) → %s", (name, type, kind) => {
    expect(resolveArtifactViewer(name, type)).toBe(kind);
  });

  it("텍스트 계열 판정", () => {
    expect(isTextyArtifact("a.json", "application/json")).toBe(true);
    expect(isTextyArtifact("a.md", "application/octet-stream")).toBe(true);
    expect(isTextyArtifact("a.mp3", "audio/mpeg")).toBe(false);
  });
});
