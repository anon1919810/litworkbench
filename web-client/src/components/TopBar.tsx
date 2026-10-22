import { useAppState } from '../state/AppState';
import { IconInfo, IconMenu } from './Icons';
import { summarizeCoverage } from '../lib/coverage';
import { viewTitle } from './Sidebar';

interface TopBarProps {
  navOpen: boolean;
  onToggleNav: () => void;
  onAbout: () => void;
  onGoSearch: () => void;
}

export function TopBar({ navOpen, onToggleNav, onAbout, onGoSearch }: TopBarProps) {
  const { workspace, coverage, view } = useAppState();
  const summary = summarizeCoverage(coverage);

  return (
    <header className="topbar">
      <button
        type="button"
        className="tb-menu"
        onClick={onToggleNav}
        aria-label={navOpen ? '关闭导航' : '打开导航'}
        aria-controls="sidebar"
        aria-expanded={navOpen}
      >
        <IconMenu size={16} />
      </button>
      <nav className="crumbs" aria-label="面包屑">
        <span>{workspace?.name?.trim() || '个人图书馆'}</span>
        <span aria-hidden="true">/</span>
        <span className="crumb-cur">{viewTitle(view)}</span>
      </nav>
      <div className="tb-right">
        <button type="button" className="tb-btn" onClick={onGoSearch} title="回到全库检索（Ctrl/Cmd + K）">
          全库检索
        </button>
        <span className="tb-note">{summary.text}</span>
        <button type="button" className="tb-icon" onClick={onAbout} aria-label="本版本的范围与限制">
          <IconInfo size={16} />
        </button>
      </div>
    </header>
  );
}