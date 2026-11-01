import type { components } from "@biznotes/shared-types";

type Schemas = components["schemas"];

export type Note = Schemas["NoteOut"];
export type NoteOrigin = Note["origin"];

export const NOTE_ORIGIN_LABEL: Record<NoteOrigin, string> = {
  manual: "직접 작성",
  chat: "채팅에서 저장",
  agent: "에이전트 결과",
};

/** `note.origin !== "manual"`일 때만 채워진다(서버가 manual에서는 항상 null로 저장). */
export type Provenance = NonNullable<Note["provenance"]>;
export type ProvenanceMode = Provenance["mode"];

export const NOTE_PROVENANCE_MODE_LABEL: Record<ProvenanceMode, string> = {
  autonomous: "자율 채팅",
  static: "정적 채팅",
  agent_run: "에이전트 실행",
  unrecorded: "생성 근거 미기록",
};
