import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { costPer1k, useAdminModels } from "@/hooks/use-admin-llm";
import type { AdminModel, AdminProvider } from "@/lib/admin-types";
import { CreateModelDialog } from "./create-model-dialog";
import { EditModelDialog } from "./edit-model-dialog";

/**
 * 이미지 생성 모델의 장당 가격 `cost_per_1k.per_image`(코어 v0.23.0 #338, core-v0.23.0-upgrade AC-3).
 * 네트워크 경계(`fetch`)만 대체하고 요청 본문을 그대로 대조한다 — `useAdminModels`·api client는 실경로다.
 */

const PROVIDER: AdminProvider = {
  id: "provider-1",
  name: "mockp",
  type: "mock",
  base_url: null,
  has_api_key: false,
  enabled: true,
  model_count: 0,
  embedding_max_concurrency: null,
};

const IMAGE_MODEL = {
  id: "m-img",
  provider_id: PROVIDER.id,
  provider_name: PROVIDER.name,
  model_name: "mock-image",
  display_name: "Mock Image",
  capabilities: ["image"],
  is_default: true,
  enabled: true,
  context_window: null,
  fallback_priority: null,
  cost_per_1k: { per_image: 0.04 },
} as AdminModel;

let sent: { method: string; url: string; body: Record<string, unknown> }[] = [];

function stubFetch() {
  sent = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase();
      const url = String(input);
      if (method !== "GET") {
        const body = JSON.parse(String(init?.body));
        sent.push({ method, url, body });
        return new Response(JSON.stringify({ ...IMAGE_MODEL, ...body }), {
          status: method === "POST" ? 201 : 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
}

function withClient(ui: React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function CreateHarness() {
  return <CreateModelDialog providers={[PROVIDER]} create={useAdminModels().create} />;
}

function EditHarness({ model }: { model: AdminModel }) {
  const [open, setOpen] = useState(true);
  return <EditModelDialog model={model} update={useAdminModels().update} open={open} onOpenChange={setOpen} />;
}

const box = (dialog: HTMLElement, modality: string) =>
  within(dialog).getByRole("checkbox", { name: new RegExp(`^${modality}\\b`) });

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("costPer1k", () => {
  it("장당 가격은 image 능력일 때만 싣고, 빈 값은 키를 뺀다", () => {
    expect(costPer1k("", "", "0.04", ["image"])).toEqual({ per_image: 0.04 });
    expect(costPer1k("0.1", "", "0.04", ["llm"])).toEqual({ input: 0.1 });
    expect(costPer1k("", "", "", ["image"])).toEqual({});
    expect(costPer1k("0.1", "0.2")).toEqual({ input: 0.1, output: 0.2 });
  });
});

describe("CreateModelDialog — 장당 가격", () => {
  it("image를 고르면 「장당 가격」이 나타나고 per_image로 보낸다", async () => {
    stubFetch();
    const user = userEvent.setup();
    withClient(<CreateHarness />);
    await user.click(screen.getByRole("button", { name: "모델 추가" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("combobox", { name: "프로바이더" }));
    await user.click(await screen.findByRole("option", { name: "mockp (mock)" }));
    await user.type(await within(dialog).findByLabelText("모델명 (LiteLLM 식별자)"), "mock-image");
    await user.type(within(dialog).getByLabelText("표시명"), "Mock Image");
    expect(within(dialog).queryByLabelText(/장당 가격/)).toBeNull();

    await user.click(box(dialog, "image"));
    expect(box(dialog, "llm")).not.toBeChecked();
    // 소수 넷째 자리 가격도 막히지 않는다(리뷰 M1 — 고정 step이면 네이티브 검증이 제출을 막는다)
    await user.type(within(dialog).getByLabelText(/장당 가격/), "0.0416");
    await user.click(within(dialog).getByRole("button", { name: "추가" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].method).toBe("POST");
    expect(sent[0].body).toEqual({
      provider_id: PROVIDER.id,
      model_name: "mock-image",
      display_name: "Mock Image",
      capabilities: ["image"],
      cost_per_1k: { per_image: 0.0416 },
    });
  });
});

describe("EditModelDialog — 장당 가격", () => {
  it("저장된 per_image를 채우고, 고친 값을 그대로 보낸다", async () => {
    stubFetch();
    const user = userEvent.setup();
    withClient(<EditHarness model={IMAGE_MODEL} />);
    const dialog = await screen.findByRole("dialog");
    const input = within(dialog).getByLabelText(/장당 가격/);
    expect(input).toHaveValue(0.04);

    expect(input).toBeValid();
    await user.clear(input);
    await user.type(input, "0.0832");
    expect(input).toBeValid();
    await user.click(within(dialog).getByRole("button", { name: "저장" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].method).toBe("PATCH");
    expect(sent[0].url).toMatch(/\/api\/v1\/admin\/llm\/models\/m-img$/);
    expect(sent[0].body.cost_per_1k).toEqual({ per_image: 0.0832 });
  });

  it("image 능력을 빼면 입력이 사라지고 per_image를 보내지 않는다", async () => {
    stubFetch();
    const user = userEvent.setup();
    withClient(<EditHarness model={IMAGE_MODEL} />);
    const dialog = await screen.findByRole("dialog");

    await user.click(box(dialog, "llm"));
    expect(within(dialog).queryByLabelText(/장당 가격/)).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "저장" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].body.capabilities).toEqual(["llm"]);
    expect(sent[0].body.cost_per_1k).toEqual({});
  });
});

describe("EditModelDialog — 최대 출력·사고 양 기본값 (코어 v0.29.0 #428·#429)", () => {
  it("저장된 값을 채우고, 고친 최대 출력과 사고 양을 보낸다", async () => {
    stubFetch();
    const user = userEvent.setup();
    const model = { ...IMAGE_MODEL, capabilities: ["llm"], max_output_tokens: 8192, reasoning_effort: "low" } as AdminModel;
    withClient(<EditHarness model={model} />);
    const dialog = await screen.findByRole("dialog");
    const maxOutput = within(dialog).getByLabelText("최대 출력 (토큰, 선택)");
    expect(maxOutput).toHaveValue(8192);
    expect(within(dialog).getByRole("combobox", { name: "사고 양 기본값" })).toHaveTextContent("낮음 (low)");

    await user.clear(maxOutput);
    await user.type(maxOutput, "16384");
    await user.click(within(dialog).getByRole("combobox", { name: "사고 양 기본값" }));
    await user.click(await screen.findByRole("option", { name: "높음 (high)" }));
    await user.click(within(dialog).getByRole("button", { name: "저장" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].body.max_output_tokens).toBe(16384);
    expect(sent[0].body.reasoning_effort).toBe("high");
  });

  it("비우면 null(해제)을 보낸다", async () => {
    stubFetch();
    const user = userEvent.setup();
    const model = { ...IMAGE_MODEL, capabilities: ["llm"], max_output_tokens: 8192, reasoning_effort: "medium" } as AdminModel;
    withClient(<EditHarness model={model} />);
    const dialog = await screen.findByRole("dialog");
    await user.clear(within(dialog).getByLabelText("최대 출력 (토큰, 선택)"));
    await user.click(within(dialog).getByRole("combobox", { name: "사고 양 기본값" }));
    await user.click(await screen.findByRole("option", { name: "기본값 없음" }));
    await user.click(within(dialog).getByRole("button", { name: "저장" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].body.max_output_tokens).toBeNull();
    expect(sent[0].body.reasoning_effort).toBeNull();
  });
});
