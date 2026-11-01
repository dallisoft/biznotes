import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { AdminModel, AdminProvider } from "@/lib/admin-types";
import { buildFallbackChain, FallbackChainSummary } from "./fallback-chain-summary";

function provider(overrides: Partial<AdminProvider> = {}): AdminProvider {
  return {
    id: "p-claude",
    name: "claude",
    type: "anthropic",
    base_url: null,
    has_api_key: true,
    enabled: true,
    model_count: 2,
    embedding_max_concurrency: null,
    ...overrides,
  } as AdminProvider;
}

function model(overrides: Partial<AdminModel> = {}): AdminModel {
  return {
    id: "m-x",
    provider_id: "p-claude",
    provider_name: "claude",
    model_name: "anthropic/x",
    display_name: "x",
    capabilities: ["llm"],
    is_default: false,
    enabled: true,
    context_window: null,
    fallback_priority: null,
    cost_per_1k: {},
    ...overrides,
  } as AdminModel;
}

const PROVIDERS = [
  provider(),
  provider({ id: "p-gemini", name: "gemini", type: "google" }),
  provider({ id: "p-mock", name: "mock", type: "mock" }),
];

// 어드민 화면 실례: 기본 sonnet에 폴백 3, opus 1, gemini 2, 비활성 mock.
const MOCK = model({ id: "m-mock", provider_id: "p-mock", display_name: "Mock", enabled: false });
const OPUS = model({ id: "m-opus", display_name: "claude-opus-5-5", fallback_priority: 1 });
const SONNET = model({
  id: "m-sonnet",
  display_name: "claude-sonnet-5-5",
  is_default: true,
  fallback_priority: 3,
});
const GEMINI = model({
  id: "m-gemini",
  provider_id: "p-gemini",
  display_name: "gemini-3.8-flash",
  fallback_priority: 2,
});

const names = (ms: AdminModel[]) => ms.map((m) => m.display_name);

describe("buildFallbackChain — 코어 resolve_chain 규칙", () => {
  it("기본 모델은 폴백 순서(3)와 상관없이 1차이고, 뒤 후보에서 빠진다", () => {
    const chain = buildFallbackChain([MOCK, OPUS, SONNET, GEMINI], PROVIDERS);
    expect(chain.primary?.display_name).toBe("claude-sonnet-5-5");
    expect(names(chain.fallbacks)).toEqual(["claude-opus-5-5", "gemini-3.8-flash"]);
    expect(chain.duplicatePriorities).toEqual([]);
    expect(chain.hasUsable).toBe(true);
  });

  it("기본 모델을 opus로 바꾸면 sonnet은 폴백 3으로 뒤에 붙는다 — 번호를 다시 매기지 않아도 된다", () => {
    const chain = buildFallbackChain(
      [{ ...OPUS, is_default: true }, { ...SONNET, is_default: false }, GEMINI],
      PROVIDERS,
    );
    expect(chain.primary?.display_name).toBe("claude-opus-5-5");
    expect(names(chain.fallbacks)).toEqual(["gemini-3.8-flash", "claude-sonnet-5-5"]);
  });

  it("비활성 모델·비활성 프로바이더의 모델·폴백 순서가 없는 모델·텍스트 생성이 아닌 모델은 후보가 아니다", () => {
    const chain = buildFallbackChain(
      [
        SONNET,
        { ...MOCK, fallback_priority: 1 },
        { ...GEMINI, fallback_priority: 2 },
        model({ id: "m-none", display_name: "no-priority" }),
        model({ id: "m-emb", display_name: "emb", capabilities: ["embedding"], fallback_priority: 4 }),
        OPUS,
      ],
      [provider(), provider({ id: "p-gemini", enabled: false }), provider({ id: "p-mock" })],
    );
    expect(names(chain.fallbacks)).toEqual(["claude-opus-5-5"]);
  });

  it("기본 모델이 비활성이면 1차가 없다(게이트웨이는 표시명 순 첫 활성 모델을 쓴다)", () => {
    const chain = buildFallbackChain([{ ...SONNET, enabled: false }, OPUS], PROVIDERS);
    expect(chain.primary).toBeNull();
    expect(chain.hasUsable).toBe(true);
  });

  it("후보끼리 겹친 폴백 순서 번호를 모은다 — 1차 모델과 겹친 번호는 세지 않는다", () => {
    const chain = buildFallbackChain(
      [{ ...SONNET, fallback_priority: 1 }, OPUS, { ...GEMINI, fallback_priority: 1 }],
      PROVIDERS,
    );
    expect(chain.duplicatePriorities).toEqual([1]);
    expect(buildFallbackChain([{ ...SONNET, fallback_priority: 1 }, OPUS], PROVIDERS).duplicatePriorities).toEqual(
      [],
    );
  });
});

describe("FallbackChainSummary", () => {
  it("1차(기본) → 폴백 순서대로 목록을 보이고, 기본 모델 번호가 다른 모델이 1차일 때만 쓰인다고 설명한다", () => {
    render(<FallbackChainSummary models={[MOCK, OPUS, SONNET, GEMINI]} providers={PROVIDERS} />);
    const region = screen.getByRole("region", { name: "텍스트 생성 실행 순서" });
    const list = within(region).getByRole("list", { name: "기본 모델로 시작하는 폴백 시도 순서" });
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "claude-sonnet-5-5기본",
      "claude-opus-5-5폴백 1",
      "gemini-3.8-flash폴백 2",
    ]);
    expect(region).toHaveTextContent("기본 모델에 적은 폴백 순서는 다른 모델이 1차일 때만 쓰입니다");
    // 도구 호출 턴은 폴백 체인을 쓰지 않는다(코어 complete_with_tools) — 이 순서를 채팅 전체에 약속하지 않는다.
    expect(region).toHaveTextContent(
      "채팅의 도구 호출 단계와 도구를 쓰는 「LLM 생성」 노드는 폴백 없이 1차 모델만 씁니다",
    );
    expect(region).not.toHaveTextContent("폴백 후보가 없어");
    expect(region).not.toHaveTextContent("같은 폴백 순서 번호");
  });

  it("폴백 후보가 없으면 기본 모델 실패가 곧 요청 실패라고 알린다", () => {
    render(<FallbackChainSummary models={[SONNET]} providers={PROVIDERS} />);
    expect(screen.getByText("폴백 후보가 없어 기본 모델이 실패하면 요청도 실패합니다.")).toBeInTheDocument();
  });

  it("겹친 번호를 알린다", () => {
    render(
      <FallbackChainSummary
        models={[SONNET, OPUS, { ...GEMINI, fallback_priority: 1 }]}
        providers={PROVIDERS}
      />,
    );
    expect(screen.getByText(/같은 폴백 순서 번호\(1\)를 여러 모델이 쓰고 있어/)).toBeInTheDocument();
  });

  it("활성 기본 모델이 없으면 목록 대신 기본 모델 지정을 안내하고, 쓸 모델이 없으면 실패를 알린다", () => {
    const { unmount } = render(
      <FallbackChainSummary models={[{ ...SONNET, is_default: false }, OPUS]} providers={PROVIDERS} />,
    );
    expect(screen.queryByRole("list")).toBeNull();
    expect(screen.getByText(/활성 기본 모델이 없어/)).toHaveTextContent("「기본 모델로 지정」");
    unmount();

    render(<FallbackChainSummary models={[MOCK]} providers={PROVIDERS} />);
    expect(screen.getByText("활성 텍스트 생성 모델이 없어 요청이 실패합니다.")).toBeInTheDocument();
  });

  it("텍스트 생성 모델이 하나도 없으면 아무것도 그리지 않는다", () => {
    const { container } = render(
      <FallbackChainSummary
        models={[model({ capabilities: ["embedding"], is_default: true })]}
        providers={PROVIDERS}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
