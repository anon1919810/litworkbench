import type { ReactNode } from 'react';
import {
  IconChevron,
  IconExcerpts,
  IconJobs,
  IconLibrary,
  IconProjects,
  IconSearch,
  IconSettings,
} from './Icons';
import { useSidebarStats } from '../hooks/useSidebarStats';
import { useAppState } from '../state/AppState';
import type { ViewId } from '../state/AppState';

interface NavEntry {
  id: ViewId;
  label: string;
  icon: (props: { size?: number }) => ReactNode;
  description: string;
}

export const NAV_WORKSPACE: NavEntry[] = [
  { id: 'notes', label: '笔记工作区', icon: IconProjects, description: '页面、研究笔记与关联引用。' },
  { id: 'search', label: '全库检索', icon: IconSearch, description: '跨全部文献检索原文，回到原页核对。' },
  { id: 'library', label: '文献库', icon: IconLibrary, description: '导入原件、查看处理状态与覆盖。' },
  { id: 'jobs', label: '识别任务', icon: IconJobs, description: '逐页文字识别的真实进度与失败页。' },
  { id: 'excerpts', label: '摘录集', icon: IconExcerpts, description: '保存的原文切片、分类与核验状态。' },
  { id: 'settings', label: '设置', icon: IconSettings, description: '本地资源与后续功能说明。' },
];

export const NAV_TOPIC: NavEntry[] = [
  { id: 'projects', label: '研究项目', icon: IconProjects, description: '专题组织与 AI 研究（后续里程碑）。' },
];

export function viewTitle(view: ViewId): string {
  return [...NAV_WORKSPACE, ...NAV_TOPIC].find((entry) => entry.id === view)?.label ?? '个人图书馆';
}

interface SidebarProps {
  onNavigate: (view: ViewId) => void;
  onClose: () => void;
  onAbout: () => void;
}

function NavList({ entries, view, onNavigate, stats }: {
  entries: NavEntry[];
  view: ViewId;
  onNavigate: (view: ViewId) => void;
  stats: { documentsTotal: number | null; jobsTotal: number | null; excerptsTotal: number | null };
}) {
  return (
    <nav aria-label="主导航">
      {entries.map((entry) => {
        const Icon = entry.icon;
        const badge =
          entry.id === 'library'
            ? stats.documentsTotal
            : entry.id === 'jobs'
              ? stats.jobsTotal
              : entry.id === 'excerpts'
                ? stats.excerptsTotal
                : null;
        return (
          <button
            key={entry.id}
            type="button"
            className="nav-item"
            aria-current={view === entry.id ? 'page' : undefined}
            onClick={() => onNavigate(entry.id)}
          >
            <span className="nav-ico">
              <Icon size={16} />
            </span>
            <span className="nav-txt">{entry.label}</span>
            {badge ? <span className="nav-badge num">{badge}</span> : null}
          </button>
        );
      })}
    </nav>
  );
}

export function Sidebar({ onNavigate, onClose, onAbout }: SidebarProps) {
  const { view, workspace } = useAppState();
  const stats = useSidebarStats();
  const name = workspace?.name?.trim() || '个人图书馆';
  // 小屏上侧栏是抽屉：选完一项就收起。
  const go = (target: ViewId) => {
    onNavigate(target);
    onClose();
  };

  return (
    <aside className="sidebar" id="sidebar">
      <button type="button" className="ws-btn" aria-label={`工作区：${name}`} onClick={() => go('search')}>
        <span className="ws-avatar" aria-hidden="true">
          {name.slice(0, 1)}
        </span>
        <span className="ws-name">{name}</span>
        <IconChevron size={12} />
      </button>
      <div className="sb-scroll">
        <p className="sb-group">工作区</p>
        <NavList entries={NAV_WORKSPACE} view={view} onNavigate={go} stats={stats} />
        <p className="sb-group">专题</p>
        <NavList entries={NAV_TOPIC} view={view} onNavigate={go} stats={stats} />
      </div>
      <div className="sb-foot">
        <p className="sb-row">
          <span className="live-dot" aria-hidden="true" />
          {workspace?.mode === 'cloud' ? '个人云端工作区' : '本地工作区'}
        </p>
        <p className="sb-row" style={{ cursor: 'default' }}>
          <span className="state-tag">开发版</span>
          <span className="sb-muted" style={{ padding: 0 }}>
            {stats.jobsActive ? `${stats.jobsActive} 个任务进行中` : '无进行中任务'}
          </span>
        </p>
        <button type="button" className="sb-row" onClick={onAbout}>
          本版本的范围与限制
        </button>
      </div>
    </aside>
  );
}
