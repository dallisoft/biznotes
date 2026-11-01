import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  candidateErrorLabel,
  defaultSelection,
  offerLicenseLabel,
  SkillInstallCard,
} from "@/components/chat/skill-install-card";
import { MessageList } from "@/components/chat/message-list";
import type { SkillInstallCandidate, SkillInstallOffer } from "@/lib/chat-types";

// 네트워크 경계(api.post)만 대역이다. ApiError는 실제 클래스를 쓴다(카드가 instanceof로 422 본문을 읽는다).
vi.mock("@/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...actual, api: { ...actual.api, post: vi.fn() } };
});
vi.mock("@/components/chat/mermaid-diagram", () => ({ MermaidDiagram: () => null }));
Element.prototype.scrollIntoView = vi.fn();

import { api, ApiError } from "@/lib/api-client";

const mockPost = vi.mocked(api.post);
const SHA = "b37e0206d31fffd9149bf57168d4d62eb2086bee";

function candidate(path: string, name: string, extra: Partial<SkillInstallCandidate> = {}): SkillInstallCandidate {
  return {
    path,
    name,
    description: `${name} 설명`,
    file_count: 1,
    ok: true,
    errors: [],
    warnings: [],
    runtime_profile: "standard_agent_v1",
    has_scripts: false,
    copy_of: null,
    reinstall: null,
    ...extra,
  };
}

function makeOffer(extra: Partial<SkillInstallOffer> = {}): SkillInstallOffer {
  return {
    owner: "epoko77-ai",
    repo: "im-not-ai",
    sha: SHA,
    ref: "main",
    archive_sha256: "a".repeat(64),
    archive_root: `im-not-ai-${SHA}`,
    subpath: null,
    license: { spdx_id: "MIT", name: "MIT License" },
    truncated: 0,
    max_paths: 10,
    candidates: [
      candidate("codex/skills/humanize-korean", "humanize-korean", {
        ok: false,
        errors: ["symlink"],
        copy_of: "skills/humanize-korean",
      }),
      candidate("copilot/skills/humanize-korean", "humanize-korean", { copy_of: "skills/humanize-korean" }),
      candidate("extras/skills/commit-ko", "commit-ko"),
      candidate("skills/humanize", "humanize", {
        reinstall: { skill_id: "s-old", name: "humanize", latest_version: 1 },
      }),
      candidate("skills/humanize-korean", "humanize-korean", {
        warnings: [
          { layer: "capability", code: "no_bundled_scripts", message: "번들 스크립트가 없습니다" },
          { layer: "product", code: "references_outside_skill", message: "스킬 폴더 밖 경로를 참조합니다(scripts/)" },
        ],
      }),
      candidate("skills/humanize-redo", "humanize-redo"),
      candidate("skills/humanize-scan", "humanize-scan"),
    ],
    ...extra,
  };
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

afterEach(() => mockPost.mockReset());

describe("SkillInstallCard", () => {
  it("출처·라이선스·대표 후보 5개를 보이고, 같은 이름의 사본은 접어 체크하지 않는다 (AC-1·AC-10)", () => {
    render(<SkillInstallCard offer={makeOffer()} />, { wrapper });

    expect(screen.getByTestId("skill-install-source")).toHaveTextContent("epoko77-ai/im-not-ai@b37e020");
    expect(screen.getByTestId("skill-install-license")).toHaveTextContent("MIT");
    const card = screen.getByTestId("skill-install-card");
    const copies = screen.getByTestId("skill-install-copies");
    const top = within(card)
      .getAllByTestId("skill-install-candidate")
      .filter((row) => !copies.contains(row));
    expect(top.map((row) => row.dataset.path)).toEqual([
      "extras/skills/commit-ko",
      "skills/humanize",
      "skills/humanize-korean",
      "skills/humanize-redo",
      "skills/humanize-scan",
    ]);
    for (const row of top) expect(within(row).getByRole("checkbox")).toBeChecked();
    expect(copies).not.toHaveAttribute("open");
    for (const path of ["codex/skills/humanize-korean", "copilot/skills/humanize-korean"]) {
      expect(within(copies).getByRole("checkbox", { name: `humanize-korean (${path}) 설치` })).not.toBeChecked();
    }
    expect(within(copies).getByRole("checkbox", { name: /codex/ })).toHaveAttribute("aria-disabled", "true");
    expect(within(copies).getByText(/심볼릭 링크가 있어 설치할 수 없음/)).toBeInTheDocument();
    // 런타임 호환 경고와 재설치 표시(AC-10·AC-8)
    expect(screen.getByText("번들 스크립트가 없습니다")).toBeInTheDocument();
    expect(screen.getByText("스킬 폴더 밖 경로를 참조합니다(scripts/)")).toBeInTheDocument();
    expect(screen.getByText(/다시 설치 — v1 다음 version/)).toBeInTheDocument();
    expect(screen.getByTestId("skill-install-submit")).toHaveTextContent("설치 (5)");
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("라이선스가 없으면 「라이선스 없음」을 보이고 설치는 막지 않는다 (AC-9)", () => {
    render(<SkillInstallCard offer={makeOffer({ license: null })} />, { wrapper });
    expect(screen.getByTestId("skill-install-license")).toHaveTextContent("라이선스 없음");
    expect(screen.getByTestId("skill-install-submit")).toBeEnabled();
  });

  it("고른 경로만 보내고, 결과에 스킬 링크·version·스크립트 안내를 보인다", async () => {
    const user = userEvent.setup();
    mockPost.mockResolvedValue({
      owner: "epoko77-ai",
      repo: "im-not-ai",
      sha: SHA,
      items: [
        {
          path: "skills/humanize",
          status: "reinstalled",
          skill_id: "s-old",
          name: "humanize",
          version: 2,
          has_scripts: true,
          reason: null,
          message: null,
        },
      ],
    });
    render(<SkillInstallCard offer={makeOffer()} />, { wrapper });
    for (const name of ["commit-ko", "humanize-korean", "humanize-redo", "humanize-scan"]) {
      const row = screen
        .getAllByTestId("skill-install-candidate")
        .find((r) => r.dataset.path === (name === "commit-ko" ? "extras/skills/commit-ko" : `skills/${name}`));
      await user.click(within(row!).getByRole("checkbox"));
    }
    await user.click(screen.getByTestId("skill-install-submit"));

    expect(mockPost).toHaveBeenCalledWith("/api/v1/skill-installs/github", {
      owner: "epoko77-ai",
      repo: "im-not-ai",
      sha: SHA,
      archive_sha256: "a".repeat(64),
      archive_root: `im-not-ai-${SHA}`,
      paths: ["skills/humanize"],
    });
    const result = await screen.findByTestId("skill-install-result");
    expect(within(result).getByRole("link", { name: "humanize" })).toHaveAttribute("href", "/skills/s-old");
    expect(result).toHaveTextContent("v2 (새 version)");
    expect(result).toHaveTextContent("스크립트 실행 허가 요청은 스킬 화면에서");
    expect(screen.queryByTestId("skill-install-submit")).not.toBeInTheDocument();
  });

  it("하나도 설치하지 못한 422는 경로별 사유를 보인다", async () => {
    const user = userEvent.setup();
    mockPost.mockRejectedValue(
      new ApiError(422, "[]", {
        detail: {
          code: "digest_mismatch",
          detail: "확인 카드를 띄운 뒤 저장소 archive가 바뀌었습니다.",
          items: [
            {
              path: "skills/humanize",
              status: "failed",
              skill_id: null,
              name: null,
              version: null,
              has_scripts: false,
              reason: "digest_mismatch",
              message: "확인 카드를 띄운 뒤 저장소 archive가 바뀌었습니다.",
            },
          ],
        },
      }),
    );
    render(<SkillInstallCard offer={makeOffer({ candidates: [candidate("skills/humanize", "humanize")] })} />, {
      wrapper,
    });
    await user.click(screen.getByTestId("skill-install-submit"));
    expect(await screen.findByText("확인 카드를 띄운 뒤 저장소 archive가 바뀌었습니다.")).toBeInTheDocument();
  });

  it("다른 오류는 메시지로, 상한을 넘기거나 아무것도 고르지 않으면 버튼을 막는다", async () => {
    const user = userEvent.setup();
    mockPost.mockRejectedValue(new ApiError(429, "요청이 너무 많습니다"));
    const many = Array.from({ length: 3 }, (_, i) => candidate(`skills/s${i}`, `s${i}`));
    render(<SkillInstallCard offer={makeOffer({ candidates: many, max_paths: 2, truncated: 4 })} />, { wrapper });
    expect(defaultSelection(makeOffer({ candidates: many, max_paths: 2 }))).toEqual(["skills/s0", "skills/s1"]);
    expect(screen.getByText(/4개는 보이지 않습니다/)).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "s2 설치" }));
    expect(screen.getByTestId("skill-install-submit")).toBeDisabled();
    expect(screen.getByText("한 번에 2개까지 설치할 수 있습니다.")).toBeInTheDocument();
    for (const name of ["s0", "s1", "s2"]) await user.click(screen.getByRole("checkbox", { name: `${name} 설치` }));
    expect(screen.getByTestId("skill-install-submit")).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "s0 설치" }));
    await user.click(screen.getByTestId("skill-install-submit"));
    expect(await screen.findByRole("alert")).toHaveTextContent("설치하지 못했습니다 — 요청이 너무 많습니다");
  });

  it("표시 문구 헬퍼", () => {
    expect(candidateErrorLabel("symlink")).toBe("심볼릭 링크가 있어 설치할 수 없음");
    expect(candidateErrorLabel("weird_code")).toBe("weird_code");
    expect(offerLicenseLabel({ license: { spdx_id: "NOASSERTION", name: "Other" } })).toBe("Other");
    expect(offerLicenseLabel({ license: { spdx_id: null, name: null } })).toBe("라이선스 확인 필요");
  });
});

describe("MessageList — 설치 확인 카드", () => {
  it("installOffer가 있는 답변 아래에만 카드를 그린다", () => {
    render(
      <MessageList
        messages={[
          { id: "u1", role: "user", content: "설치해줘", citations: [] },
          { id: "a1", role: "assistant", content: "후보 5개를 찾았습니다", citations: [], installOffer: makeOffer() },
          { id: "a2", role: "assistant", content: "다른 답변", citations: [] },
        ]}
        canWrite
        streamingContent=""
        onAddSource={() => {}}
        onSaveNote={() => {}}
      />,
      { wrapper },
    );
    expect(screen.getAllByTestId("skill-install-card")).toHaveLength(1);
  });
});

it("설치 중에는 버튼이 「설치 중…」이고 체크를 바꿀 수 없다", async () => {
  const user = userEvent.setup();
  let resolve: (v: unknown) => void = () => {};
  mockPost.mockReturnValue(new Promise((r) => (resolve = r)) as never);
  render(<SkillInstallCard offer={makeOffer({ candidates: [candidate("skills/humanize", "humanize")] })} />, {
    wrapper,
  });
  await user.click(screen.getByTestId("skill-install-submit"));
  await waitFor(() => expect(screen.getByTestId("skill-install-submit")).toHaveTextContent("설치 중…"));
  expect(screen.getByRole("checkbox", { name: "humanize 설치" })).toHaveAttribute("aria-disabled", "true");
  resolve({ owner: "o", repo: "r", sha: SHA, items: [] });
  await screen.findByTestId("skill-install-result");
});
