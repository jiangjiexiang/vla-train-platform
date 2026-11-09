import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import {
  api,
  clearStoredTokens,
  getErrorMessage,
  getSessionToken,
  getStaticToken,
  isUnauthorized,
  storeSessionToken,
  storeStaticToken,
  UNAUTHORIZED_EVENT,
} from '../api/client';
import type { AuthMode, CurrentUser } from '../types/api';

export const UNAUTHENTICATED_MESSAGE = '登录已失效，请重新登录';

interface AuthState {
  token: string | null;
  // 健康检查是否成功；null 表示仍在探测
  authRequired: boolean | null;
  // /health 返回的 auth_mode（ldap/token/off）；旧版后端不返回时按 auth_required 推断
  authMode: AuthMode | null;
  // /auth/me 的结果；匿名（off）模式下降级为本地构造的匿名用户
  user: CurrentUser | null;
  message: string | null;
}

interface AuthContextValue extends AuthState {
  isAuthenticated: boolean;
  isAdmin: boolean;
  // token 模式的静态共享令牌
  loginWithToken: (token: string) => Promise<void>;
  // ldap 模式的用户名 + 密码
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// 匿名模式下的等价用户（契约 1.2）：{"username": "anonymous", "role": "admin", "source": "off"}
export const ANONYMOUS_USER: CurrentUser = { username: 'anonymous', role: 'admin', source: 'off' };

// 优先信任显式的 auth_mode，其次按 v0.8 的 auth_required 推断；两者都没有时按 off 处理
function resolveAuthMode(authRequired: boolean | undefined, authMode: AuthMode | undefined): AuthMode {
  if (authMode) return authMode;
  if (authRequired === false) return 'off';
  if (authRequired === true) return 'token';
  return 'off';
}

function readTokenForMode(mode: AuthMode | null): string | null {
  if (mode === 'ldap') return getSessionToken();
  if (mode === 'token') return getStaticToken() ?? getSessionToken();
  return null;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<AuthMode | null>(null);
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [tokenVersion, setTokenVersion] = useState(0);
  const [message, setMessage] = useState<string | null>(null);

  const token = useMemo(() => readTokenForMode(mode), [mode, tokenVersion]);
  const authRequired = mode === null ? null : mode !== 'off';

  const clearSession = useCallback((nextMessage: string | null = null) => {
    clearStoredTokens();
    setUser(null);
    setMessage(nextMessage);
    setTokenVersion((value) => value + 1);
  }, []);

  const refreshMe = useCallback(async (targetMode: AuthMode) => {
    if (targetMode === 'off') {
      setUser(ANONYMOUS_USER);
      return;
    }
    if (!readTokenForMode(targetMode)) {
      setUser(null);
      return;
    }
    try {
      const current = await api.me();
      setUser(current);
      setMessage(null);
    } catch (reason) {
      setUser(null);
      // 401 由 request() 派发 UNAUTHORIZED_EVENT，这里只兜住其他错误（如 /auth/me 尚未部署）
      if (!isUnauthorized(reason)) {
        setMessage(getErrorMessage(reason));
      }
    }
  }, []);

  // 启动探测：/health 的 auth_required + auth_mode 决定是否需要登录页
  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const health = await api.getHealth();
        if (!active) return;
        const nextMode = resolveAuthMode(health.auth_required, health.auth_mode);
        setMode(nextMode);
        if (nextMode === 'off') {
          // 匿名模式：清掉残留令牌，直接以匿名 admin 身份进入，不展示登录页
          clearStoredTokens();
          setTokenVersion((value) => value + 1);
          setUser(ANONYMOUS_USER);
          return;
        }
        await refreshMe(nextMode);
      } catch (reason) {
        // 健康检查失败时按「需要登录」处理，登录页会提示后端不可用
        if (!active) return;
        setMode((previous) => previous ?? 'token');
        setMessage(getErrorMessage(reason));
      }
    })();
    return () => {
      active = false;
    };
  }, [refreshMe]);

  // 任意接口返回 401：清空令牌，ProtectedRoutes 会带着 /health 的结论跳回登录页
  useEffect(() => {
    const onUnauthorized = () => clearSession(UNAUTHENTICATED_MESSAGE);
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [clearSession]);

  const loginWithToken = useCallback(
    async (value: string) => {
      const nextToken = value.trim();
      if (!nextToken) throw new Error('请输入访问令牌');
      storeStaticToken(nextToken);
      setTokenVersion((version) => version + 1);
      try {
        // /config 需要鉴权，可用来校验令牌是否有效
        await api.getConfig(undefined, nextToken);
        // 错误的共享令牌不会被 /auth/me 识别；识别不到时退化为契约中的 token 用户
        const current = await api.me().catch((reason: unknown) => {
          if (isUnauthorized(reason)) return { username: 'token', role: 'admin', source: 'token' } as CurrentUser;
          throw reason;
        });
        setUser(current);
        setMessage(null);
          setMode((previous) => previous ?? 'token');
      } catch (reason) {
        clearSession(null);
        throw reason;
      }
    },
    [clearSession],
  );

  const login = useCallback(async (username: string, password: string) => {
    const payload = await api.login(username.trim(), password);
    storeSessionToken(payload.token);
    setTokenVersion((version) => version + 1);
    setMessage(null);
    setMode((previous) => previous ?? 'ldap');
    if (payload.user) {
      setUser(payload.user);
    } else {
      await refreshMe('ldap');
    }
  }, [refreshMe]);

  const logout = useCallback(async () => {
    // 尽力通知服务端撤销令牌，失败（离线/已过期）不影响本地退出
    if (getSessionToken()) {
      await api.logout().catch(() => undefined);
    }
    clearSession(null);
  }, [clearSession]);

  const isAuthenticated = authRequired === false || token !== null;
  const isAdmin = user?.role === 'admin';

  const value = useMemo<AuthContextValue>(
    () => ({
      token,
      authRequired,
      authMode: mode,
      user,
      message,
      isAuthenticated,
      isAdmin,
      loginWithToken,
      login,
      logout,
    }),
    [token, authRequired, mode, user, message, isAuthenticated, isAdmin, loginWithToken, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth 必须在 AuthProvider 中使用');
  return context;
}
