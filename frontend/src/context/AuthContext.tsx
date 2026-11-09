import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { api, TOKEN_STORAGE_KEY, UNAUTHORIZED_EVENT } from '../api/client';

interface AuthContextValue {
  token: string | null;
  authRequired: boolean | null;
  isAuthenticated: boolean;
  login: (token: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function clearStoredToken() {
  localStorage.removeItem(TOKEN_STORAGE_KEY);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => localStorage.getItem(TOKEN_STORAGE_KEY));
  const [authRequired, setAuthRequired] = useState<boolean | null>(null);

  const logout = useCallback(() => {
    clearStoredToken();
    setToken(null);
  }, []);

  useEffect(() => {
    let active = true;
    api.getHealth()
      .then((health) => {
        if (!active) return;
        const required = health.auth_required !== false;
        setAuthRequired(required);
        if (!required) logout();
      })
      .catch(() => {
        if (active) setAuthRequired(true);
      });
    return () => {
      active = false;
    };
  }, [logout]);

  useEffect(() => {
    window.addEventListener(UNAUTHORIZED_EVENT, logout);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, logout);
  }, [logout]);

  const login = useCallback(async (value: string) => {
    const nextToken = value.trim();
    await api.getConfig(undefined, nextToken);
    localStorage.setItem(TOKEN_STORAGE_KEY, nextToken);
    setToken(nextToken);
  }, []);

  const isAuthenticated = authRequired === false || token !== null;
  const value = useMemo(
    () => ({ token, authRequired, isAuthenticated, login, logout }),
    [token, authRequired, isAuthenticated, login, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth 必须在 AuthProvider 中使用');
  return context;
}
