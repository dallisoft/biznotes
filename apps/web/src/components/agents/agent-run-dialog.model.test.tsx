/**
 * 스튜디오 실행 모델 선택(#216, core-v0.25.0-upgrade AC-12 · D4) — AgentRunDialog·RunModelSelect·조회/생성 훅을 실제로
 * 돌리고 API 경계(`api` 객체)만 대체한다. 기본값 「기본 모델」이면 요청에 `model_id`가 없고(지금처럼 시스템 기본
 * 모델), 고르면 그 모델 id를 보낸다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { defaultRunModelLabel } from "@/components/agents/run-model-select";
import type { Agent } from "@/lib/agent-types";
import type { LLMModelInfo } from "@/lib/chat-types";
import { STUDIO_CARDS } from "@/lib/studio-registry";

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), getText: vi.fn(), post: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const { AgentRunDialog } = await import("@/components/agents/agent-run-dialog");

const AGENT = {
  id: "agent1",
  name: "마인드맵",
  description: "소스 내용을 구조화합니다",
  icon: "🧩",
  kind: "skill",
  params_schema: { type: "object", properties: {} },
  scope: "system",
  enabled: true,
  version: 1,
} as unknown as Agent;

function model(id: string, display_name: string, is_default = false): LLMModelInfo {
  return { id, display_name, provider_name: "mock", provider_type: "mock", is_default };
}

const MODELS = [model("m-default", "gpt-oss-120b", true), model("m-flash", "gemini 3.8 flash")];

function renderDialog(models: LLMModelInfo[] = MODELS) {
  api.get.mockImplementation((path: string) => {
    if (path === "/api/v1/models") return Promise.resolve(models);
    if (path === "/api/v1/notebooks/nb1/sources") return Promise.resolve([]);
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
  api.post.mockResolvedValue({ id: "run-1", status: "queued" });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onOpenChange = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <AgentRunDialog
        agent={AGENT}
        notebookId="nb1"
        open
        onOpenChange={onOpenChange}
        studioCard={STUDIO_CARDS[0]}
      />
    </QueryClientProvider>,
  );
  return { onOpenChange };
}

async function pickModel(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(screen.getByRole("combobox", { name: "모델" }));
  await user.click(await screen.findByRole("option", { name }));
}

beforeEach(() => {
  api.get.mockReset();
  api.post.mockReset();
});

describe("defaultRunModelLabel — 「기본 모델」 항목 라벨", () => {
  it("is_default 모델 이름을 괄호로, 없으면 목록 첫 모델(코어 resolve_model 순서), 비었으면 이름 없이", () => {
    expect(defaultRunModelLabel(MODELS)).toBe("기본 모델 (gpt-oss-120b)");
    expect(defaultRunModelLabel([model("a", "첫 모델"), model("b", "둘째")])).toBe("기본 모델 (첫 모델)");
    expect(defaultRunModelLabel([])).toBe("기본 모델");
    expect(defaultRunModelLabel(undefined)).toBe("기본 모델");
  });
});

describe("AgentRunDialog — 실행 모델 선택(#216)", () => {
  it("기본값은 「기본 모델」이고, 그대로 생성하면 요청에 model_id가 없다", async () => {
    const user = userEvent.setup();
    renderDialog();
    const trigger = screen.getByRole("combobox", { name: "모델" });
    await waitFor(() => expect(trigger).toHaveTextContent("기본 모델 (gpt-oss-120b)"));

    await user.click(screen.getByTestId("run-agent"));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/nb1/agent-runs", {
      agent_id: "agent1",
      params: {},
    });
  });

  it("목록은 「기본 모델」 다음에 켜진 llm 모델(GET /api/v1/models) — 고르면 그 id를 보내고 닫힌 트리거에 이름이 보인다", async () => {
    const user = userEvent.setup();
    const { onOpenChange } = renderDialog();
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "모델" })).toHaveTextContent("기본 모델 (gpt-oss-120b)"),
    );
    await user.click(screen.getByRole("combobox", { name: "모델" }));
    const options = (await screen.findAllByRole("option")).map((o) => o.textContent);
    expect(options).toEqual(["기본 모델 (gpt-oss-120b)", "gpt-oss-120b", "gemini 3.8 flash"]);
    await user.click(screen.getByRole("option", { name: "gemini 3.8 flash" }));
    // 닫힌 트리거는 UUID가 아니라 모델 이름을 보인다
    expect(screen.getByRole("combobox", { name: "모델" })).toHaveTextContent("gemini 3.8 flash");

    await user.click(screen.getByTestId("run-agent"));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/nb1/agent-runs", {
      agent_id: "agent1",
      params: {},
      model_id: "m-flash",
    });
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  });

  it("모델을 골랐다가 「기본 모델」로 되돌리면 다시 model_id를 보내지 않는다", async () => {
    const user = userEvent.setup();
    renderDialog();
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "모델" })).toHaveTextContent("기본 모델 (gpt-oss-120b)"),
    );
    await pickModel(user, "gemini 3.8 flash");
    await pickModel(user, "기본 모델 (gpt-oss-120b)");

    await user.click(screen.getByTestId("run-agent"));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post.mock.calls[0][1]).not.toHaveProperty("model_id");
  });

  it("고를 모델이 없으면(목록 비어 있음) 「기본 모델」 하나만 있고 model_id 없이 생성한다", async () => {
    const user = userEvent.setup();
    renderDialog([]);
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/api/v1/models"));
    await user.click(screen.getByRole("combobox", { name: "모델" }));
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["기본 모델"]);
    await user.keyboard("{Escape}");

    await user.click(screen.getByTestId("run-agent"));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post.mock.calls[0][1]).not.toHaveProperty("model_id");
  });
});
