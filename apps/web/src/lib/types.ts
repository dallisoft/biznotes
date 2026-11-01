// API 응답 타입 (apps/api Pydantic 스키마 정본 — pnpm gen:types로 생성)
import type { components } from "@biznotes/shared-types";

type Schemas = components["schemas"];

export type User = Schemas["UserOut"];
export type Notebook = Schemas["NotebookOut"];
export type Tag = Schemas["TagOut"];
export type NotebookShare = Schemas["NotebookShareOut"];

export type NotebookFilter = "all" | "mine" | "shared";
