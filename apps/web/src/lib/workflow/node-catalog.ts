/**
 * 노드 타입 디스크립터 레지스트리 — 팔레트·속성 폼·클라이언트 검증의 단일 소스
 * (workflow-visual-editor-plan §2). 런타임 NODE_EXECUTORS(apps/api runtime.py)와 1:1.
 */
import {
  Bot,
  Braces,
  Database,
  FileDown,
  FileText,
  Globe,
  Library,
  type LucideIcon,
  Plug,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Users,
  Workflow,
} from "lucide-react";

export type NodeType =
  | "constant"
  | "rag_search"
  | "source_load"
  | "llm"
  | "save_artifact"
  | "save_sources"
  | "http"
  | "mcp_tool"
  | "agent_call"
  | "sub_workflow"
  | "error_handler"
  | "supervisor"
  | "react_agent";

export type NodeCategory = "input" | "ai" | "tool" | "output" | "subagent";

export type FieldControl =
  | "text"
  | "textarea"
  | "prompt"
  | "number"
  | "select"
  | "multiselect"
  | "json"
  | "mcp-tool-picker"
  | "agent-picker";

export interface NodeField {
  key: string;
  label: string;
  control: FieldControl;
  placeholder?: string;
  help?: string;
  options?: { value: string; label: string }[];
  required?: boolean;
  /** 보여 주기만 하고 고칠 수 없는 리터럴(관리자가 시드·관리 API로 정한 값) */
  readOnly?: boolean;
}

export interface NodeTypeDescriptor {
  type: NodeType;
  category: NodeCategory;
  label: string;
  icon: LucideIcon;
  color: string;
  description: string;
  fields: NodeField[];
  defaults: Record<string, unknown>;
  /** 출력이 없는(후속 노드가 참조 불가) 노드 — 엣지 호환 힌트용 */
  terminal?: boolean;
}

export const CATEGORY_LABEL: Record<NodeCategory, string> = {
  input: "입력",
  ai: "AI",
  tool: "도구",
  output: "출력",
  subagent: "서브에이전트",
};

export const CATEGORY_ORDER: NodeCategory[] = ["input", "ai", "tool", "output", "subagent"];

/**
 * 외부 출력 안내(core-v0.20.0-upgrade §5.6 — 코어 v0.20.0 #228 `{경로|untrusted}`). 코어는 LLM 프롬프트
 * (llm `system`·`prompt`, react `system`·`goal`, supervisor 종합)에서 필터 값을 `kind="tool"` 데이터 블록으로
 * 감싸고, 그 밖(HTTP·MCP 인자·검색어·하위 실행 params)에서는 원값을 넣는다. `{id.context}`(생산자 블록)와
 * 사람이 쓴 `{params.*}` 지시에는 달지 않는다 — 웹훅으로 외부 값을 받는 파라미터만 작성자가 단다.
 */
export const UNTRUSTED_PROMPT_HELP =
  "외부에서 온 출력(HTTP·MCP·하위 실행의 {id.text}, 검색 인용 {id.citations}, 웹훅 입력)은 {id.text|untrusted}처럼 넣으세요";

/** 외부 출력을 내는 노드(http·mcp_tool·하위 실행·supervisor) 설명의 출력 표기 뒤에 붙는 같은 안내. */
export const UNTRUSTED_OUTPUT_NOTE = "외부에서 온 출력이라 프롬프트에는 {id.text|untrusted}처럼 넣으세요";

const PROMPT_HELP = `{params.x}, {노드id.text} 처럼 이전 노드 출력을 참조할 수 있습니다. ${UNTRUSTED_PROMPT_HELP}`;

/**
 * 산출물 도구(코어 v0.10.12 #133 — `artifact_list`·`artifact_read`, 읽기 전용 opt-in). 같은 노트북에서
 * 실행 사용자가 볼 수 있는 다른 실행의 산출물을 읽는다(HCI-11 ④, HCI-12).
 */
const ARTIFACT_TOOL_OPTIONS: { value: string; label: string }[] = [
  { value: "artifact_list", label: "산출물 목록" },
  { value: "artifact_read", label: "산출물 읽기" },
];
const ARTIFACT_TOOLS_HELP =
  "산출물 목록·읽기 도구는 같은 노트북의 다른 실행 산출물을 읽습니다(실행 사용자가 볼 수 있는 것만).";

export const NODE_CATALOG: Record<NodeType, NodeTypeDescriptor> = {
  rag_search: {
    type: "rag_search",
    category: "input",
    label: "RAG 검색",
    icon: Database,
    color: "#3b82f6",
    description: "노트북 소스를 의미 검색해 관련 발췌를 가져옵니다 → {id.context}",
    defaults: { query: "{params.topic}", top_k: 6 },
    fields: [
      {
        key: "query",
        label: "검색어",
        control: "prompt",
        placeholder: "{params.topic}",
        help: PROMPT_HELP,
        required: true,
      },
      { key: "top_k", label: "가져올 발췌 수", control: "number", help: "기본 6" },
    ],
  },
  source_load: {
    type: "source_load",
    category: "input",
    label: "소스 원문",
    icon: FileText,
    color: "#0ea5e9",
    description: "소스 전문을 그대로 주입합니다(전문 모드) → {id.context}",
    defaults: { max_sources: 5, max_chars: 60000 },
    fields: [
      { key: "max_sources", label: "최대 소스 수", control: "number", help: "기본 5" },
      { key: "max_chars", label: "최대 글자 수", control: "number", help: "기본 60000" },
    ],
  },
  constant: {
    type: "constant",
    category: "input",
    label: "상수",
    icon: Braces,
    color: "#14b8a6",
    description: "고정 값을 상태에 주입합니다(템플릿 치환 없음)",
    defaults: { value: {} },
    fields: [
      {
        key: "value",
        label: "값(JSON)",
        control: "json",
        help: "객체면 키들이 {id.키}로, 아니면 {id.value}로 참조됩니다",
      },
    ],
  },
  llm: {
    type: "llm",
    category: "ai",
    label: "LLM 생성",
    icon: Sparkles,
    color: "#8b5cf6",
    description: "프롬프트로 LLM을 호출해 텍스트를 생성합니다 → {id.text}",
    defaults: { system: "", prompt: "{search.context}" },
    fields: [
      {
        key: "system",
        label: "시스템 프롬프트",
        control: "textarea",
        placeholder: "역할·지침을 설명하세요(선택)",
      },
      {
        key: "prompt",
        label: "프롬프트",
        control: "prompt",
        placeholder: "{search.context} 를 요약해줘",
        help: PROMPT_HELP,
        required: true,
      },
      { key: "max_tokens", label: "최대 토큰", control: "number", help: "기본 4096" },
      {
        key: "prompt_ref",
        label: "프롬프트 템플릿 ID",
        control: "text",
        placeholder: "(선택) 버전 관리되는 프롬프트 템플릿 id",
        help: "지정 시 템플릿의 활성 버전이 프롬프트를 대체합니다",
      },
      {
        key: "tools",
        label: "도구 사용",
        control: "multiselect",
        help: "선택 시 모델이 스스로 도구를 호출합니다(네이티브 tool-calling).",
        // 읽기 전용 도구만 — llm 노드 도구 루프에는 승인 게이트가 없어 부작용 도구(http_request·
        // mcp_call·call_agent 등)는 저장 시 422·실행 시 실패다(코어 v0.10.3 #70). 부작용 도구는
        // 승인 게이트가 있는 react_agent의 "허용 도구"에서 쓴다.
        options: [
          { value: "rag_search", label: "RAG 검색" },
          { value: "web_search", label: "웹 검색" },
        ],
      },
    ],
  },
  http: {
    type: "http",
    category: "tool",
    label: "HTTP 요청",
    icon: Globe,
    color: "#f59e0b",
    description: `외부 HTTP API를 호출합니다 → {id.text}, {id.status} (${UNTRUSTED_OUTPUT_NOTE})`,
    defaults: { url: "https://", method: "GET" },
    fields: [
      {
        key: "url",
        label: "URL",
        control: "prompt",
        placeholder: "https://api.example.com/{params.id}",
        required: true,
      },
      {
        key: "method",
        label: "메서드",
        control: "select",
        options: ["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => ({ value: m, label: m })),
      },
      { key: "headers", label: "헤더(JSON)", control: "json" },
      { key: "body", label: "본문(JSON)", control: "json" },
    ],
  },
  mcp_tool: {
    type: "mcp_tool",
    category: "tool",
    label: "MCP 도구",
    icon: Plug,
    color: "#10b981",
    description: `등록된 MCP 서버의 도구를 호출합니다 → {id.text} (${UNTRUSTED_OUTPUT_NOTE})`,
    defaults: { server_id: "", tool_name: "", arguments: {} },
    fields: [
      { key: "server_id", label: "MCP 서버 / 도구", control: "mcp-tool-picker", required: true },
      { key: "arguments", label: "인자(JSON)", control: "json", help: PROMPT_HELP },
    ],
  },
  agent_call: {
    type: "agent_call",
    category: "subagent",
    label: "에이전트 호출",
    icon: Bot,
    color: "#ec4899",
    description: `다른 워크플로우/스킬을 서브에이전트로 호출합니다 → {id.text} (${UNTRUSTED_OUTPUT_NOTE})`,
    defaults: { agent_id: "", params: {} },
    fields: [
      { key: "agent_id", label: "대상 에이전트", control: "agent-picker", required: true },
      {
        key: "params",
        label: "파라미터 매핑(JSON)",
        control: "json",
        help: "대상 파라미터에 {params.x} 등을 매핑합니다",
      },
    ],
  },
  sub_workflow: {
    type: "sub_workflow",
    category: "subagent",
    label: "서브워크플로우",
    icon: Workflow,
    color: "#d946ef",
    description: `다른 워크플로우를 입출력 매핑과 함께 호출합니다 → {id.text} (${UNTRUSTED_OUTPUT_NOTE})`,
    defaults: { workflow_id: "", input_map: {}, output_key: "" },
    fields: [
      { key: "workflow_id", label: "대상 워크플로우", control: "agent-picker", required: true },
      {
        key: "input_map",
        label: "입력 매핑(JSON)",
        control: "json",
        help: "대상 파라미터 ← 상태 표현식. 예: {\"topic\": \"{params.subject}\"}",
      },
      {
        key: "output_key",
        label: "출력 키",
        control: "text",
        placeholder: "sub_out",
        help: "결과를 {id.<출력키>}로도 노출(선택)",
      },
    ],
  },
  error_handler: {
    type: "error_handler",
    category: "subagent",
    label: "에러 핸들러",
    icon: ShieldAlert,
    color: "#ef4444",
    description: `보호 대상을 재시도(백오프)로 감싸고 실패 시 폴백 에이전트를 실행합니다 → {id.text} (${UNTRUSTED_OUTPUT_NOTE})`,
    defaults: {
      protected_agent_id: "",
      params: {},
      max_retries: 1,
      backoff: 1,
      fallback_agent_id: "",
      fallback_params: {},
    },
    fields: [
      {
        key: "protected_agent_id",
        label: "보호 대상 에이전트",
        control: "agent-picker",
        required: true,
      },
      { key: "params", label: "보호 대상 파라미터(JSON)", control: "json", help: PROMPT_HELP },
      { key: "max_retries", label: "최대 재시도", control: "number", help: "기본 1" },
      { key: "backoff", label: "백오프(초)", control: "number", help: "재시도 간 대기. 기본 1" },
      {
        key: "fallback_agent_id",
        label: "폴백 에이전트",
        control: "agent-picker",
        help: "보호 대상이 최종 실패하면 실행(선택)",
      },
      { key: "fallback_params", label: "폴백 파라미터(JSON)", control: "json" },
    ],
  },
  supervisor: {
    type: "supervisor",
    category: "subagent",
    label: "슈퍼바이저",
    icon: Users,
    color: "#a855f7",
    description: `여러 워커 에이전트에 팬아웃 후 결과를 종합합니다(토론/협업) → {id.text} (${UNTRUSTED_OUTPUT_NOTE})`,
    defaults: { workers: [], parallel: true, synthesis_prompt: "" },
    fields: [
      {
        key: "workers",
        label: "워커 목록(JSON)",
        control: "json",
        required: true,
        help: '[{"agent_id":"…","label":"워커A","params":{}}, …]',
      },
      {
        key: "synthesis_prompt",
        label: "종합 프롬프트",
        control: "prompt",
        placeholder: "{results|untrusted} 를 종합해줘",
        help: "비우면 워커 결과를 단순 결합. 워커 출력은 외부에서 온 출력이라 {results|untrusted}처럼 참조하세요",
      },
    ],
  },
  react_agent: {
    type: "react_agent",
    category: "subagent",
    label: "자율 에이전트 (ReAct)",
    icon: ShieldCheck,
    color: "#f97316",
    description: "LLM이 도구를 스스로 선택해 목표를 달성합니다(부작용 도구는 승인 필요) → {id.text}",
    defaults: { goal: "{params.topic}", allowed_tools: ["rag_search", "call_agent"], max_iters: 6 },
    fields: [
      { key: "goal", label: "목표", control: "prompt", required: true, help: PROMPT_HELP },
      { key: "system", label: "시스템 프롬프트", control: "textarea" },
      {
        key: "allowed_tools",
        label: "허용 도구",
        control: "multiselect",
        help: `미지정 시 기본값(rag_search·call_agent)만 노출. call_agent·http_request 등 부작용 도구는 실행 시 사용자 승인이 필요합니다. ${ARTIFACT_TOOLS_HELP}`,
        options: [
          { value: "rag_search", label: "RAG 검색" },
          { value: "call_agent", label: "서브에이전트 호출" },
          { value: "web_search", label: "웹 검색" },
          { value: "http_request", label: "HTTP 요청" },
          { value: "mcp_call", label: "MCP 도구(단건 지정)" },
          ...ARTIFACT_TOOL_OPTIONS,
        ],
      },
      { key: "max_iters", label: "최대 반복", control: "number", help: "기본 6, 상한 12" },
      { key: "token_budget", label: "토큰 예산", control: "number", help: "기본 100,000, 상한 200,000" },
      {
        key: "cost_budget_usd",
        label: "비용 예산(USD)",
        control: "number",
        help: "기본 0.50, 상한 2.00",
      },
      {
        key: "max_subagent_calls",
        label: "서브에이전트 호출 상한",
        control: "number",
        help: "기본 5, 상한 10",
      },
      {
        key: "auto_approve_tools",
        label: "자동 승인 정책(JSON)",
        control: "json",
        help: '도구별 인자 조건에 맞으면 승인 없이 자동 실행. 예: {"http_request":{"domains":["api.example.com"],"methods":["GET"]},"call_agent":{"agent_ids":["…"]}}',
      },
      { key: "model_id", label: "모델 ID", control: "text", help: "(선택) 특정 모델 지정" },
    ],
  },
  save_sources: {
    type: "save_sources",
    category: "output",
    label: "소스로 저장",
    icon: Library,
    color: "#0ea5e9",
    description:
      "목록(JSON 배열)의 항목을 노트북 소스로 만듭니다 → {id.text}(생성·건너뜀·실패 수). 이미 있는 항목(같은 중복 키)은 건너뜁니다. 중복 키 이름공간·허용 호스트·노트북 규칙은 관리자가 정한 값이라 여기서 고칠 수 없습니다",
    defaults: { items: "{list.text}" },
    fields: [
      {
        key: "items",
        label: "항목(JSON 배열)",
        control: "prompt",
        placeholder: "{list.text}",
        help: "앞 노드 출력이 title·external_id와 url(PDF) 또는 content를 가진 항목의 JSON 배열이어야 합니다",
        required: true,
      },
      { key: "namespace", label: "중복 키 이름공간", control: "text", readOnly: true },
      { key: "allowed_hosts", label: "PDF 허용 호스트", control: "json", readOnly: true },
      { key: "notebook_rule", label: "노트북 규칙", control: "json", readOnly: true },
    ],
  },
  save_artifact: {
    type: "save_artifact",
    category: "output",
    label: "아티팩트 저장",
    icon: FileDown,
    color: "#64748b",
    description: "결과를 파일 아티팩트로 저장합니다",
    defaults: { name: "result.md", content: "{generate.text}", content_type: "text/markdown" },
    terminal: true,
    fields: [
      { key: "name", label: "파일명", control: "text", placeholder: "result.md", required: true },
      {
        key: "content",
        label: "내용",
        control: "prompt",
        placeholder: "{generate.text}",
        help: PROMPT_HELP,
        required: true,
      },
      {
        key: "content_type",
        label: "콘텐츠 타입",
        control: "select",
        options: [
          { value: "text/markdown", label: "Markdown" },
          { value: "text/plain", label: "Plain text" },
          { value: "application/json", label: "JSON" },
          { value: "text/html", label: "HTML" },
        ],
      },
    ],
  },
};

export const NODE_TYPES = Object.keys(NODE_CATALOG) as NodeType[];

/**
 * 편집 표면 — 노드·옵션 숨김의 판정 입력이다. 로그인 사용자의 role이 아니다(계획 AC-30, D-25).
 *
 * - `user`: 일반 워크플로우 에디터(`/workflows`). admin이 저장해도 정의 scope는 `"user"`이므로
 *   코어 v0.10.6 `find_forbidden_mcp_tool_nodes`가 `mcp_tool` 노드와 react_agent의 `mcp_call`을
 *   저장 시 422로 거부한다 — 고르면 거부될 항목을 내지 않는다.
 * - `admin`: 관리자 빌더·관리 표면(system 정의, `POST /admin/agents` 기본 scope `system`) — 전부 낸다.
 *
 * 숨김이지 삭제가 아니다: 디스크립터는 그대로라 기존 정의의 `mcp_tool` 노드도 렌더·요약된다.
 */
export type EditorSurface = "user" | "admin";

const USER_EDITOR_HIDDEN_NODE_TYPES: ReadonlySet<NodeType> = new Set<NodeType>(["mcp_tool"]);
/**
 * 어느 표면 팔레트에도 내지 않는 노드 — 리터럴(이름공간·허용 호스트·노트북 규칙)을 이 폼에서 정할 수
 * 없어 새로 만들면 저장이 거부된다. 시드·관리 API로 만든 노드를 렌더하고 `items`만 고친다(#260).
 */
const NO_PALETTE_NODE_TYPES: ReadonlySet<NodeType> = new Set<NodeType>(["save_sources"]);
const USER_EDITOR_HIDDEN_OPTIONS: Partial<Record<NodeType, Record<string, ReadonlySet<string>>>> = {
  react_agent: { allowed_tools: new Set(["mcp_call"]) },
};

/**
 * 관리자 빌더(`components/admin/workflow-builder.tsx`) 팔레트 — 런타임 노드 타입과 1:1(apps/api
 * runtime.NODE_EXECUTORS). system 정의를 다루는 관리 표면이라 `mcp_tool`을 낸다.
 */
export const ADMIN_BUILDER_NODE_TYPES = [
  "rag_search",
  "llm",
  "http",
  "mcp_tool",
  "save_artifact",
] as const satisfies readonly NodeType[];

/** 표면이 새로 추가할 수 있게 내는 노드 타입(팔레트). */
export function paletteNodeTypes(surface: EditorSurface): NodeType[] {
  const creatable = NODE_TYPES.filter((t) => !NO_PALETTE_NODE_TYPES.has(t));
  if (surface === "admin") return creatable;
  return creatable.filter((t) => !USER_EDITOR_HIDDEN_NODE_TYPES.has(t));
}

function forSurface(desc: NodeTypeDescriptor, surface: EditorSurface): NodeTypeDescriptor {
  const hidden = surface === "user" ? USER_EDITOR_HIDDEN_OPTIONS[desc.type] : undefined;
  if (!hidden) return desc;
  return {
    ...desc,
    fields: desc.fields.map((f) => {
      const drop = hidden[f.key];
      return drop && f.options ? { ...f, options: f.options.filter((o) => !drop.has(o.value)) } : f;
    }),
  };
}

const SURFACE_CATALOG: Record<EditorSurface, Record<NodeType, NodeTypeDescriptor>> = {
  user: Object.fromEntries(
    NODE_TYPES.map((t) => [t, forSurface(NODE_CATALOG[t], "user")]),
  ) as Record<NodeType, NodeTypeDescriptor>,
  admin: NODE_CATALOG,
};

/**
 * 표면에 맞춘 디스크립터. 기본값 `user`는 일반 에디터의 속성 패널(`property-panel.tsx`, 이
 * 에디터 전용)이 쓰는 값이다 — 관리 표면은 `"admin"`을 넘기거나 `NODE_CATALOG`를 직접 읽는다.
 */
export function getDescriptor(
  type: string,
  surface: EditorSurface = "user",
): NodeTypeDescriptor | undefined {
  return SURFACE_CATALOG[surface][type as NodeType];
}

/** 노드의 1줄 요약(커스텀 노드 렌더용) — 핵심 필드 값을 짧게 */
export function summarizeNode(node: Record<string, unknown>): string {
  const type = String(node.type);
  switch (type) {
    case "rag_search":
      return String(node.query ?? "");
    case "llm":
      return String(node.prompt ?? "");
    case "http":
      return `${node.method ?? "GET"} ${node.url ?? ""}`;
    case "mcp_tool":
      return String(node.tool_name ?? node.server_id ?? "");
    case "save_artifact":
      return String(node.name ?? "");
    case "save_sources": {
      const rule = node.notebook_rule as { title_template?: unknown } | undefined;
      return String(rule?.title_template ?? node.namespace ?? "");
    }
    case "agent_call":
      return node.agent_id ? "서브에이전트" : "(대상 미지정)";
    case "sub_workflow":
      return node.workflow_id ? "서브워크플로우" : "(대상 미지정)";
    case "error_handler":
      return node.protected_agent_id
        ? node.fallback_agent_id
          ? "보호 + 폴백"
          : "보호(재시도)"
        : "(대상 미지정)";
    case "supervisor": {
      const n = Array.isArray(node.workers) ? node.workers.length : 0;
      return n ? `워커 ${n}` : "(워커 미지정)";
    }
    case "react_agent":
      return String(node.goal ?? node.prompt ?? "");
    default:
      return "";
  }
}

/** 노드별 필드 required 검증 — 누락 필드 메시지 목록 반환 */
export function validateNodeFields(node: Record<string, unknown>): string[] {
  const desc = getDescriptor(String(node.type));
  if (!desc) return [`알 수 없는 노드 타입: ${node.type}`];
  const errors: string[] = [];
  for (const f of desc.fields) {
    if (!f.required) continue;
    const v = node[f.key];
    if (v === undefined || v === null || v === "") {
      errors.push(`${node.id}: '${f.label}' 필수 입력`);
    }
  }
  return errors;
}
