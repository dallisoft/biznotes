import { create } from "zustand";

interface CommandPaletteState {
  open: boolean;
  setOpen: (open: boolean) => void;
}

// 셸 레일·대시보드 버튼·Cmd+K 단축키가 공유하는 팔레트 열림 상태
export const useCommandPalette = create<CommandPaletteState>((set) => ({
  open: false,
  setOpen: (open) => set({ open }),
}));
