import type { ReactNode } from 'react';

interface PageHeaderProps {
  title: string;
  description: ReactNode;
  icon: ReactNode;
  compact?: boolean;
}

export function PageHeader({ title, description, icon, compact = false }: PageHeaderProps) {
  return (
    <div className={compact ? 'page-head sm' : 'page-head'}>
      <div className="page-icon" aria-hidden="true">
        {icon}
      </div>
      <h1 className="page-title">{title}</h1>
      <p className="page-desc">{description}</p>
    </div>
  );
}