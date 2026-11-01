import { create } from "zustand";

interface SourceFocusState {
  // 인용 칩 클릭 등으로 소스 패널에서 포커스해야 할 소스 id (1회성 요청)
  requestedSourceId: string | null;
  /** 함께 요청한 인용 발췌(청크 전문) — 상세 화면이 원문에서 이 구간을 찾아 표시한다(ui-ux-improvements ②). */
  requestedQuote: string | null;
  request: (sourceId: string, quote?: string | null) => void;
  clear: () => void;
}

/** [Phase 18c] 인용 클릭 → 소스 패널의 해당 소스로 스크롤/펼침 포커스 요청. */
export const useSourceFocus = create<SourceFocusState>((set) => ({
  requestedSourceId: null,
  requestedQuote: null,
  request: (sourceId, quote = null) => set({ requestedSourceId: sourceId, requestedQuote: quote }),
  clear: () => set({ requestedSourceId: null, requestedQuote: null }),
}));
