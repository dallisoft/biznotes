"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";

export const COLOR_THEMES = ["indigo", "teal", "neutral"] as const;
export type ColorTheme = (typeof COLOR_THEMES)[number];

const STORAGE_KEY = "color-theme";
const DEFAULT_THEME: ColorTheme = "indigo";

interface ColorThemeContextValue {
  colorTheme: ColorTheme;
  setColorTheme: (theme: ColorTheme) => void;
}

const ColorThemeContext = createContext<ColorThemeContextValue>({
  colorTheme: DEFAULT_THEME,
  setColorTheme: () => {},
});

function applyTheme(theme: ColorTheme) {
  if (theme === DEFAULT_THEME) delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
}

export function ColorThemeProvider({ children }: { children: React.ReactNode }) {
  // SSR 시점엔 기본값 — 실제 적용은 layout.tsx의 인라인 스크립트가 페인트 전에 처리(FOUC 방지)
  const [colorTheme, setColorThemeState] = useState<ColorTheme>(DEFAULT_THEME);

  useEffect(() => {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && COLOR_THEMES.includes(stored as ColorTheme)) {
      setColorThemeState(stored as ColorTheme);
    }
  }, []);

  const setColorTheme = useCallback((theme: ColorTheme) => {
    setColorThemeState(theme);
    localStorage.setItem(STORAGE_KEY, theme);
    applyTheme(theme);
  }, []);

  return (
    <ColorThemeContext.Provider value={{ colorTheme, setColorTheme }}>
      {children}
    </ColorThemeContext.Provider>
  );
}

export function useColorTheme() {
  return useContext(ColorThemeContext);
}
