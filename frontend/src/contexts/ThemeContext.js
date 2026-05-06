import React, { createContext, useContext, useState, useCallback } from 'react';

const ThemeContext = createContext(null);

const STORAGE_KEY = 'loore_theme';

function readInitialTheme() {
  // The bootstrap script in public/index.html has already set this attribute
  // synchronously before paint, honoring localStorage and prefers-color-scheme.
  if (typeof document !== 'undefined') {
    const attr = document.documentElement.getAttribute('data-theme');
    if (attr === 'light' || attr === 'dark') return attr;
  }
  return 'dark';
}

export function ThemeProvider({ children }) {
  const [theme, setThemeState] = useState(readInitialTheme);

  const setTheme = useCallback((next) => {
    if (next !== 'light' && next !== 'dark') return;
    setThemeState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch (e) {}
    document.documentElement.setAttribute('data-theme', next);
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme(theme === 'light' ? 'dark' : 'light');
  }, [theme, setTheme]);

  return (
    <ThemeContext.Provider value={{ theme, setTheme, toggleTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error('useTheme must be used within ThemeProvider');
  return ctx;
}
