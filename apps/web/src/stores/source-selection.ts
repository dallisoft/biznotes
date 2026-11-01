import { create } from "zustand";

interface SourceSelectionState {
  // notebookId → 선택된 source id 집합 (기본: 전체 선택)
  selected: Record<string, string[]>;
  toggle: (notebookId: string, sourceId: string) => void;
  setAll: (notebookId: string, sourceIds: string[]) => void;
  isSelected: (notebookId: string, sourceId: string) => boolean;
}

export const useSourceSelection = create<SourceSelectionState>((set, get) => ({
  selected: {},
  toggle: (notebookId, sourceId) =>
    set((state) => {
      const current = state.selected[notebookId] ?? [];
      const next = current.includes(sourceId)
        ? current.filter((id) => id !== sourceId)
        : [...current, sourceId];
      return { selected: { ...state.selected, [notebookId]: next } };
    }),
  setAll: (notebookId, sourceIds) =>
    set((state) => ({
      selected: { ...state.selected, [notebookId]: sourceIds },
    })),
  isSelected: (notebookId, sourceId) =>
    (get().selected[notebookId] ?? []).includes(sourceId),
}));
