import type { ReactNode } from 'react';
import type { Tone } from '../lib/format';

const TONE_CLASS: Record<Tone, string> = {
  neutral: 'tag',
  info: 'tag tag-info',
  warn: 'tag tag-warn',
  error: 'tag tag-error',
  progress: 'tag tag-info state',
};

interface TagProps {
  tone?: Tone;
  /** 带小圆点的状态标签 */
  state?: boolean;
  stale?: boolean;
  children: ReactNode;
  title?: string;
}

export function Tag({ tone = 'neutral', state = false, stale = false, children, title }: TagProps) {
  const classes = state ? `${TONE_CLASS[tone]} state${stale ? ' state-stale' : ''}` : TONE_CLASS[tone];
  return (
    <span className={classes} title={title}>
      {children}
    </span>
  );
}