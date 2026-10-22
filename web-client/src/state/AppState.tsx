/**
 * 应用级共享状态：本地会话、覆盖统计、导航目标、提示条、数据版本号。
 * 只有确实跨页共享的东西放在这里；页面数据仍由各自 hook 获取。
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { getCoverage, getWorkspace } from '../api/endpoints';
import type { Coverage, WorkspaceInfo } from '../api/types';
import { normalizeCoveragePayload } from '../lib/coverage';
import { describeError } from '../lib/conflict';
import type { ErrorInfo } from '../lib/conflict';
import type { Tone } from '../lib/format';

export type ViewId = 'search' | 'library' | 'jobs' | 'excerpts' | 'projects' | 'settings';

export interface Toast {
  id: number;
  text: string;
  tone: Tone;
}

export interface ReaderTarget {
  unitId: string;
  /** 每次打开都自增，便于阅读区在相同单元上重新取数 */
  token: number;
}

export interface AppState {
  status: 'booting' | 'ready' | 'failed';
  workspace: WorkspaceInfo | null;
  bootError: ErrorInfo | null;
  retryBoot: () => void;

  coverage: Coverage | null;
  refreshCoverage: () => Promise<void>;
  /** 检索响应或导入后带回的真实覆盖统计 */
  applyCoverage: (coverage: Coverage) => void;

  view: ViewId;
  navigate: (view: ViewId) => void;
  readerTarget: ReaderTarget | null;
  openReader: (unitId: string) => void;
  /** 文献库“在检索中查看”：带着来源过滤条件跳到检索页 */
  docScopeTarget: ReaderTarget | null;
  openDocScope: (docId: string) => void;

  toasts: Toast[];
  pushToast: (text: string, tone?: Tone) => void;
  dismissToast: (id: number) => void;

  /** 导入、保存摘录、校订等写操作后自增，相关列表据此重新取数。 */
  dataVersion: number;
  bumpDataVersion: () => void;
  /** 手动刷新：重新取一次权威的覆盖与列表计数（前端不合成任何数值）。 */
  refreshCounts: () => void;
}

const AppContext = createContext<AppState | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AppState['status']>('booting');
  const [workspace, setWorkspace] = useState<WorkspaceInfo | null>(null);
  const [bootError, setBootError] = useState<ErrorInfo | null>(null);
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [view, setView] = useState<ViewId>(() => readStoredView());
  const [readerTarget, setReaderTarget] = useState<ReaderTarget | null>(null);
  const [docScopeTarget, setDocScopeTarget] = useState<ReaderTarget | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [dataVersion, setDataVersion] = useState(0);
  const toastId = useRef(1);
  const coverageSeq = useRef(0);

  const boot = useCallback(() => {
    setStatus('booting');
    setBootError(null);
    // 握手是全局共享的一次性操作，不能被 StrictMode 的重复挂载取消。
    let alive = true;
    getWorkspace()
      .then((info) => {
        if (!alive) return;
        setWorkspace(info);
        setCoverage(info.coverage ?? null);
        setStatus('ready');
      })
      .catch((error: unknown) => {
        if (!alive) return;
        setBootError(describeError(error));
        setStatus('failed');
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => boot(), [boot]);

  const refreshCoverage = useCallback(async () => {
    const seq = (coverageSeq.current += 1);
    try {
      const payload = await getCoverage();
      if (seq !== coverageSeq.current) return;
      const next = normalizeCoveragePayload(payload);
      if (next) setCoverage(next);
    } catch {
      // 覆盖统计是辅助信息，读取失败时保留上一次的真实数值，不伪造。
    }
  }, []);

  const applyCoverage = useCallback((next: Coverage) => setCoverage(next), []);

  const pushToast = useCallback((text: string, tone: Tone = 'neutral') => {
    const id = toastId.current;
    toastId.current += 1;
    setToasts((current) => [...current, { id, text, tone }]);
    window.setTimeout(() => setToasts((current) => current.filter((item) => item.id !== id)), 6_000);
  }, []);

  const dismissToast = useCallback((id: number) => {
    setToasts((current) => current.filter((item) => item.id !== id));
  }, []);

  const navigate = useCallback((next: ViewId) => {
    setView(next);
    try {
      window.sessionStorage.setItem('litworkbench.view.v1', next);
    } catch {
      /* 浏览器禁用存储时不影响使用 */
    }
  }, []);

  const openReader = useCallback((unitId: string) => {
    setReaderTarget({ unitId, token: Date.now() });
    navigate('search');
  }, [navigate]);

  const openDocScope = useCallback(
    (docId: string) => {
      setDocScopeTarget({ unitId: docId, token: Date.now() });
      navigate('search');
    },
    [navigate],
  );

  const bumpDataVersion = useCallback(() => setDataVersion((value) => value + 1), []);

  const refreshCounts = useCallback(() => {
    void refreshCoverage();
    setDataVersion((value) => value + 1);
  }, [refreshCoverage]);

  const value = useMemo<AppState>(
    () => ({
      status,
      workspace,
      bootError,
      retryBoot: boot,
      coverage,
      refreshCoverage,
      applyCoverage,
      view,
      navigate,
      readerTarget,
      openReader,
      docScopeTarget,
      openDocScope,
      toasts,
      pushToast,
      dismissToast,
      dataVersion,
      bumpDataVersion,
      refreshCounts,
    }),
    [
      status,
      workspace,
      bootError,
      boot,
      coverage,
      refreshCoverage,
      applyCoverage,
      view,
      navigate,
      readerTarget,
      openReader,
      docScopeTarget,
      openDocScope,
      toasts,
      pushToast,
      dismissToast,
      dataVersion,
      bumpDataVersion,
      refreshCounts,
    ],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useAppState(): AppState {
  const value = useContext(AppContext);
  if (!value) throw new Error('useAppState 必须在 AppProvider 内使用');
  return value;
}

function readStoredView(): ViewId {
  try {
    const stored = window.sessionStorage.getItem('litworkbench.view.v1');
    if (stored === 'library' || stored === 'jobs' || stored === 'excerpts' || stored === 'projects' || stored === 'settings' || stored === 'search') {
      return stored;
    }
  } catch {
    /* 忽略 */
  }
  return 'search';
}