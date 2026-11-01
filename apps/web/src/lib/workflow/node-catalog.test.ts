import { describe, expect, it } from "vitest";
import { validateDag, upstreamVariables } from "@/lib/workflow/dag";
import {
  ADMIN_BUILDER_NODE_TYPES,
  getDescriptor,
  NODE_CATALOG,
  paletteNodeTypes,
  summarizeNode,
  UNTRUSTED_OUTPUT_NOTE,
  UNTRUSTED_PROMPT_HELP,
  validateNodeFields,
} from "@/lib/workflow/node-catalog";

const SIDE_EFFECT_TOOLS = [
  "call_agent",
  "http_request",
  "mcp_call",
  "register_source",
  "run_skill_script",
];

function optionValues(type: string, key: string, surface?: "user" | "admin"): string[] {
  const field = getDescriptor(type, surface)?.fields.find((f) => f.key === key);
  return (field?.options ?? []).map((o) => o.value);
}

describe("node-catalog", () => {
  it("has a descriptor for every runtime node type", () => {
    for (const t of [
      "rag_search",
      "source_load",
      "constant",
      "llm",
      "http",
      "mcp_tool",
      "agent_call",
      "sub_workflow",
      "error_handler",
      "supervisor",
      "save_artifact",
      "save_sources",
    ]) {
      expect(getDescriptor(t)).toBeDefined();
    }
  });

  it("flags missing required fields", () => {
    const errs = validateNodeFields({ id: "n1", type: "llm" });
    expect(errs.length).toBeGreaterThan(0);
    expect(validateNodeFields({ id: "n1", type: "llm", prompt: "hi" })).toEqual([]);
  });

  it("requires target agents for subagent nodes", () => {
    expect(validateNodeFields({ id: "n1", type: "sub_workflow" }).length).toBeGreaterThan(0);
    expect(validateNodeFields({ id: "n1", type: "sub_workflow", workflow_id: "w" })).toEqual([]);
    expect(validateNodeFields({ id: "n1", type: "error_handler" }).length).toBeGreaterThan(0);
    expect(
      validateNodeFields({ id: "n1", type: "error_handler", protected_agent_id: "p" }),
    ).toEqual([]);
  });

  it("summarizes nodes", () => {
    expect(summarizeNode({ id: "a", type: "http", method: "GET", url: "u" })).toContain("GET");
    expect(summarizeNode({ id: "a", type: "agent_call" })).toContain("미지정");
    expect(summarizeNode({ id: "a", type: "sub_workflow", workflow_id: "w" })).toContain(
      "서브워크플로우",
    );
    expect(
      summarizeNode({ id: "a", type: "error_handler", protected_agent_id: "p", fallback_agent_id: "f" }),
    ).toContain("폴백");
    expect(
      summarizeNode({ id: "a", type: "supervisor", workers: [{ agent_id: "w" }] }),
    ).toContain("워커");
  });
});

describe("llm 노드 도구 옵션 (계획 AC-28 (a), 코어 v0.10.3 #70)", () => {
  it("offers only read-only tools on the llm node", () => {
    const readOnly = ["rag_search", "web_search"];
    const values = optionValues("llm", "tools");
    expect(values.sort()).toEqual(readOnly);
    for (const tool of SIDE_EFFECT_TOOLS) expect(values).not.toContain(tool);
    // 관리 표면 디스크립터도 같다 — 부작용 도구는 어느 표면에서도 llm 노드 옵션이 아니다
    expect(optionValues("llm", "tools", "admin").sort()).toEqual(readOnly);
  });

  it("keeps approval-gated tools on react_agent (descriptor keeps mcp_call for management surfaces)", () => {
    // HCI-42 (a)로 AC-28 (a) ②를 갱신: 디스크립터 options에는 call_agent·http_request·mcp_call이
    // 남고(승인 게이트 경로), 일반 에디터 노출 집합에서만 mcp_call이 빠진다(T-24).
    const descriptor = NODE_CATALOG.react_agent.fields.find((f) => f.key === "allowed_tools");
    const values = (descriptor?.options ?? []).map((o) => o.value);
    for (const tool of ["call_agent", "http_request", "mcp_call"]) expect(values).toContain(tool);
    expect(optionValues("react_agent", "allowed_tools", "admin")).toEqual(values);
  });
});

describe("편집 표면 숨김 (계획 AC-30, 코어 v0.10.6 #77·#95)", () => {
  // role 무관성 = 표면만 받는 시그니처 + admin 계정 e2e. paletteNodeTypes·getDescriptor는 로그인
  // role을 입력으로 받지 않으므로(admin도 /workflows로는 scope "user" 정의만 저장한다, 계획
  // AC-30·D-25) 두 role이 같은 "user" 표면을 본다. admin 계정 실증은
  // e2e/workflow-editor-surface.spec.ts가 맡는다.
  it("user workflow editor palette hides mcp_tool for user and admin roles", () => {
    const palette = paletteNodeTypes("user");
    expect(palette).not.toContain("mcp_tool");
    // 나머지 노드는 그대로 낸다
    expect(palette).toContain("react_agent");
    // mcp_tool(user 표면 숨김)과 save_sources(어느 팔레트에도 없음)만 빠진다
    expect(palette.length).toBe(Object.keys(NODE_CATALOG).length - 2);
    // 숨김이지 삭제가 아니다 — 기존 정의의 mcp_tool 노드는 계속 렌더·요약된다
    expect(getDescriptor("mcp_tool")).toBeDefined();
    expect(summarizeNode({ id: "m", type: "mcp_tool", tool_name: "search" })).toBe("search");
  });

  it("user workflow editor hides mcp_call from react_agent allowed_tools for user and admin roles", () => {
    const values = optionValues("react_agent", "allowed_tools");
    expect(values).not.toContain("mcp_call");
    // 승인 게이트 경로의 다른 부작용 도구는 숨기지 않는다
    expect(values).toEqual(expect.arrayContaining(["call_agent", "http_request"]));
  });

  it("admin system builder exposes mcp_tool", () => {
    expect(ADMIN_BUILDER_NODE_TYPES).toContain("mcp_tool");
    expect(paletteNodeTypes("admin")).toContain("mcp_tool");
  });
});

describe("산출물 도구 옵션 (core-v0.11.0-upgrade AC-3.6 — v7 AC-26, HCI-11 ④)", () => {
  function help(type: string, key: string, surface?: "user" | "admin"): string {
    return getDescriptor(type, surface)?.fields.find((f) => f.key === key)?.help ?? "";
  }

  it("react allowed_tools에 artifact_list·artifact_read와 도움말이 있다(llm 노드에는 없다)", () => {
    for (const surface of ["user", "admin"] as const) {
      expect(optionValues("react_agent", "allowed_tools", surface)).toEqual(
        expect.arrayContaining(["artifact_list", "artifact_read"]),
      );
      expect(help("react_agent", "allowed_tools", surface)).toContain("산출물 목록·읽기 도구");
    }
    expect(optionValues("llm", "tools")).not.toContain("artifact_list");
    expect(optionValues("llm", "tools")).not.toContain("artifact_read");
  });

  it("일반 에디터 숨김 집합은 바뀌지 않는다 — react의 mcp_call만 숨고 새 도구는 그대로 보인다", () => {
    const admin = optionValues("react_agent", "allowed_tools", "admin");
    const user = optionValues("react_agent", "allowed_tools", "user");
    expect(admin.filter((v) => !user.includes(v))).toEqual(["mcp_call"]);
    expect(optionValues("llm", "tools", "user")).toEqual(optionValues("llm", "tools", "admin"));
    expect(paletteNodeTypes("user")).not.toContain("mcp_tool");
    expect(paletteNodeTypes("user").length).toBe(Object.keys(NODE_CATALOG).length - 2);
  });
});

describe("외부 출력 |untrusted 안내 (core-v0.20.0-upgrade AC-2.7 — 계획 §5.6, 코어 v0.20.0 #228)", () => {
  // 계획 §5.6 문구 그대로 — 바꾸려면 계획을 먼저 바꾼다
  const HELP =
    "외부에서 온 출력(HTTP·MCP·하위 실행의 {id.text}, 검색 인용 {id.citations}, 웹훅 입력)은 {id.text|untrusted}처럼 넣으세요";
  // 외부 출력을 내는 노드 — http·mcp_tool과 하위 실행(agent_call·sub_workflow·error_handler)·supervisor
  const EXTERNAL_OUTPUT_NODES = [
    "http",
    "mcp_tool",
    "agent_call",
    "sub_workflow",
    "error_handler",
    "supervisor",
  ] as const;

  it("PROMPT_HELP 도움말이 계획 문구를 담고 기존 참조 안내도 유지한다", () => {
    expect(UNTRUSTED_PROMPT_HELP).toBe(HELP);
    const promptHelps = Object.values(NODE_CATALOG).flatMap((d) =>
      d.fields.filter((f) => f.control === "prompt" && f.help?.includes("{노드id.text}")),
    );
    // rag_search 검색어·llm 프롬프트·react 목표·save_artifact 내용
    expect(promptHelps.length).toBeGreaterThanOrEqual(4);
    for (const f of promptHelps) {
      expect(f.help).toContain(HELP);
      expect(f.help).toContain("{params.x}, {노드id.text} 처럼 이전 노드 출력을 참조할 수 있습니다");
    }
    // 프롬프트 필드가 아닌 PROMPT_HELP 사용처(mcp 인자·에러 핸들러 파라미터)도 같은 문구다
    expect(getDescriptor("mcp_tool", "admin")?.fields.find((f) => f.key === "arguments")?.help).toContain(HELP);
    expect(getDescriptor("error_handler")?.fields.find((f) => f.key === "params")?.help).toContain(HELP);
  });

  it("외부 출력 노드 설명의 출력 표기 뒤에 같은 안내가 붙는다(두 편집 표면 모두)", () => {
    expect(UNTRUSTED_OUTPUT_NOTE).toBe("외부에서 온 출력이라 프롬프트에는 {id.text|untrusted}처럼 넣으세요");
    for (const type of EXTERNAL_OUTPUT_NODES) {
      const desc = NODE_CATALOG[type].description;
      // 출력 표기({id.text}…) 다음에 안내가 온다
      expect(desc).toMatch(/→ \{id\.text\}.* \(외부에서 온 출력이라 프롬프트에는 \{id\.text\|untrusted\}처럼 넣으세요\)$/);
      for (const surface of ["user", "admin"] as const) {
        expect(getDescriptor(type, surface)?.description ?? "").toContain(UNTRUSTED_OUTPUT_NOTE);
      }
    }
    expect(NODE_CATALOG.http.description).toContain("→ {id.text}, {id.status} (");
  });

  it("생산자 블록·자체 생성 노드 설명에는 안내를 붙이지 않는다", () => {
    for (const type of ["rag_search", "source_load", "constant", "llm", "react_agent", "save_artifact"] as const) {
      expect(NODE_CATALOG[type].description).not.toContain("|untrusted");
    }
  });

  it("supervisor 종합 프롬프트는 워커 결과를 {results|untrusted}로 참조하라고 안내한다", () => {
    const field = NODE_CATALOG.supervisor.fields.find((f) => f.key === "synthesis_prompt");
    expect(field?.help).toContain("{results|untrusted}");
    expect(field?.placeholder).toBe("{results|untrusted} 를 종합해줘");
  });
});

describe("dag", () => {
  it("detects cycles", () => {
    const nodes = [
      { id: "a", type: "llm", prompt: "x" },
      { id: "b", type: "llm", prompt: "y" },
    ];
    const { error } = validateDag(nodes, [
      ["a", "b"],
      ["b", "a"],
    ]);
    expect(error).toContain("사이클");
  });

  it("passes a valid linear graph", () => {
    const nodes = [
      { id: "s", type: "rag_search", query: "{params.topic}" },
      { id: "g", type: "llm", prompt: "{s.context}" },
      { id: "save", type: "save_artifact", name: "out.md", content: "{g.text}" },
    ];
    const { error } = validateDag(nodes, [
      ["s", "g"],
      ["g", "save"],
    ]);
    expect(error).toBeNull();
  });

  it("warns when a terminal node has an outgoing edge", () => {
    const nodes = [
      { id: "save", type: "save_artifact", name: "out.md", content: "x" },
      { id: "g", type: "llm", prompt: "y" },
    ];
    const { warning } = validateDag(nodes, [["save", "g"]]);
    expect(warning).toContain("출력이 없어");
  });

  it("suggests upstream variables from ancestors + params", () => {
    const nodes = [
      { id: "s", type: "rag_search", query: "q" },
      { id: "g", type: "llm", prompt: "p" },
    ];
    const vars = upstreamVariables("g", nodes, [["s", "g"]], ["topic"]);
    expect(vars).toContain("{params.topic}");
    expect(vars).toContain("{s.context}");
  });
});

describe("save_sources 노드 (#260)", () => {
  it("items만 편집할 수 있고 리터럴 필드는 읽기 전용이다(두 표면 같음)", () => {
    for (const surface of ["user", "admin"] as const) {
      const desc = getDescriptor("save_sources", surface);
      expect(desc?.category).toBe("output");
      const fields = Object.fromEntries((desc?.fields ?? []).map((f) => [f.key, f]));
      expect(fields.items).toMatchObject({ control: "prompt", required: true });
      expect(fields.items.readOnly).toBeUndefined();
      for (const key of ["namespace", "allowed_hosts", "notebook_rule"]) {
        expect(fields[key]).toMatchObject({ readOnly: true });
      }
    }
  });

  it("어느 표면 팔레트에도 없다 — 리터럴을 이 폼에서 정할 수 없다", () => {
    expect(paletteNodeTypes("user")).not.toContain("save_sources");
    expect(paletteNodeTypes("admin")).not.toContain("save_sources");
    expect(paletteNodeTypes("admin")).toContain("mcp_tool");
    expect(ADMIN_BUILDER_NODE_TYPES).not.toContain("save_sources");
  });

  it("검증은 items만 요구하고 요약은 노트북 규칙 제목(없으면 이름공간)이다", () => {
    expect(validateNodeFields({ id: "s", type: "save_sources" })).toEqual([
      "s: '항목(JSON 배열)' 필수 입력",
    ]);
    expect(validateNodeFields({ id: "s", type: "save_sources", items: "{list.text}" })).toEqual([]);
    expect(
      summarizeNode({
        id: "s",
        type: "save_sources",
        namespace: "naver-research",
        notebook_rule: { title_template: "종목분석 {month}" },
      }),
    ).toBe("종목분석 {month}");
    expect(summarizeNode({ id: "s", type: "save_sources", namespace: "naver-research" })).toBe(
      "naver-research",
    );
  });

  it("뒤 노드는 {save.text}를 참조할 수 있다", () => {
    const nodes = [
      { id: "list", type: "mcp_tool" },
      { id: "save", type: "save_sources" },
      { id: "after", type: "llm" },
    ];
    const vars = upstreamVariables("after", nodes, [
      ["list", "save"],
      ["save", "after"],
    ]);
    expect(vars).toEqual(expect.arrayContaining(["{list.text}", "{save.text}"]));
  });
});
