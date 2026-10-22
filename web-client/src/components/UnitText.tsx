import { forwardRef } from 'react';

interface UnitTextProps {
  text: string;
  /** 行号式显示不可用于选择，因此这里只渲染一个纯文本节点 */
  className?: string;
}

/**
 * 识别文字的承载元素：单一文本节点 + pre-wrap 保留换行。
 * 这样 DOM Selection 给出的 UTF-16 偏移与原文一一对应，保存摘录无需反查位置。
 */
export const UnitText = forwardRef<HTMLParagraphElement, UnitTextProps>(function UnitText({ text, className }, ref) {
  return (
    <p ref={ref} className={className ?? 'unit-text'} lang="zh-CN">
      {text}
    </p>
  );
});