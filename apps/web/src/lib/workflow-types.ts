import type { components } from "@biznotes/shared-types";

type Schemas = components["schemas"];

/** definition.nodes 의 1개 노드 — id/type + 타입별 props (자유 키) */
export type WorkflowNode = Schemas["WorkflowNode"];
export type WorkflowDefinition = Schemas["WorkflowDefinition"];

export type Workflow = Schemas["WorkflowOut"];

/** agent_call(서브에이전트) 선택용 요약 */
export type CallableAgent = Schemas["CallableAgentOut"];
