import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { AboutDialog } from './components/AboutDialog';
import { CloudLoginNotice, LoginLink } from './components/CloudLogin';
import { Sidebar } from './components/Sidebar';
import { Toasts } from './components/Toasts';
import { TopBar } from './components/TopBar';
import { ExcerptsPage } from './pages/ExcerptsPage';
import { JobsPage } from './pages/JobsPage';
import { LibraryPage } from './pages/LibraryPage';
import { ProjectsPage } from './pages/ProjectsPage';
import { SearchPage } from './pages/SearchPage';
import { SettingsPage } from './pages/SettingsPage';
import { AppProvider, useAppState } from './state/AppState';

const NotesPage = lazy(() => import('./notes/NotesPage'));

function Workspace() {
  const { status, view, navigate, bootError, retryBoot } = useAppState();
  const [navOpen, setNavOpen] = useState(false);
  const [aboutOpen, setAboutOpen] = useState(false);

  const goSearch = useCallback(() => {
    navigate('search');
    setNavOpen(false);
    window.setTimeout(() => document.getElementById('global-search')?.focus(), 0);
  }, [navigate]);

  // Ctrl/Cmd + K 聚焦检索框；Esc 关闭小屏导航。均不接管浏览器原生查找。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        goSearch();
        return;
      }
      if (event.key === 'Escape' && navOpen) setNavOpen(false);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [goSearch, navOpen]);

  useEffect(() => {
    document.body.classList.toggle('nav-open', navOpen);
  }, [navOpen]);

  if (status === 'booting') {
    return (
      <div className="app">
        <div className="col">
          <div className="boot">
            <h1>个人图书馆</h1>
            <p>正在连接本机文献服务…</p>
          </div>
        </div>
      </div>
    );
  }

  if (status === 'failed') {
    return (
      <div className="app">
        <div className="col">
          <div className="boot">
            <h1>{bootError?.code === 'LOGIN_REQUIRED' ? '登录个人图书馆' : '无法连接文献服务'}</h1>
            <p>{bootError?.message ?? '请确认启动器仍在运行，然后重试。'}</p>
            <div className="btn-row" style={{ marginTop: 14 }}>
              {bootError?.code === 'LOGIN_REQUIRED' ? <LoginLink /> : null}
              <button type="button" className="btn btn-primary" onClick={retryBoot}>
                重新连接
              </button>
            </div>
            <p className="help">
              本页只访问同源的 <code>/api/v1</code>；如果刚重启过服务，重新连接会建立新的会话并取回新的写请求令牌。
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <a className="skip" href="#main">
        跳到主内容
      </a>
      <Sidebar onNavigate={navigate} onClose={() => setNavOpen(false)} onAbout={() => setAboutOpen(true)} />
      <CloudLoginNotice />
      {navOpen ? <div className="nav-scrim" onClick={() => setNavOpen(false)} aria-hidden="true" /> : null}

      <div className="col">
        <TopBar
          navOpen={navOpen}
          onToggleNav={() => setNavOpen((value) => !value)}
          onAbout={() => setAboutOpen(true)}
          onGoSearch={goSearch}
        />
        <main className={view === 'search' ? 'workspace view-search' : 'workspace'} id="main">
          {view === 'search' ? <SearchPage /> : null}
          {view === 'library' ? <LibraryPage /> : null}
          {view === 'jobs' ? <JobsPage /> : null}
          {view === 'excerpts' ? <ExcerptsPage /> : null}
          {view === 'projects' ? <ProjectsPage /> : null}
          {view === 'settings' ? <SettingsPage /> : null}
          {view === 'notes' ? <Suspense fallback={<p className="help">正在加载笔记工作区…</p>}><NotesPage /></Suspense> : null}
        </main>
      </div>

      <AboutDialog open={aboutOpen} onClose={() => setAboutOpen(false)} />
      <Toasts />
    </div>
  );
}

export default function App() {
  return (
    <AppProvider>
      <Workspace />
    </AppProvider>
  );
}
