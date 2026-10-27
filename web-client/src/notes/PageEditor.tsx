import { BlockNoteSchema, createHeadingBlockSpec, defaultBlockSpecs } from '@blocknote/core';
import { useEffect, useRef } from 'react';
import { createReactBlockSpec, useCreateBlockNote } from '@blocknote/react';
import { BlockNoteView } from '@blocknote/mantine';
import '@blocknote/mantine/style.css';
import type { NoteBody, NoteBlock } from './types';

const referenceProps = {
  pageId: { default: '' }, documentId: { default: '' }, evidenceId: { default: '' },
  title: { default: '' }, filename: { default: '' }, quote: { default: '' },
  locator: { default: '' }, review_status: { default: '' }, unit_id: { default: '' },
  effective_archived: { default: false }, missing: { default: false },
};
function reference<T extends 'pageRef' | 'documentRef' | 'evidenceRef'>(type: T) {
  return createReactBlockSpec({ type, propSchema: referenceProps, content: 'none' }, {
    render: ({ block }) => {
      const p = block.props as unknown as { [K in keyof typeof referenceProps]: typeof referenceProps[K]['default'] };
      const href = type === 'pageRef' ? `/pages/${encodeURIComponent(p.pageId)}` :
        type === 'evidenceRef' ? `/?unit=${encodeURIComponent(p.unit_id)}` : `/?doc=${encodeURIComponent(p.documentId)}`;
      return <div className="note-reference" contentEditable={false}>
        <a href={href}>{type === 'pageRef' ? '↗ 页面' : type === 'documentRef' ? '▤ 文献' : '❝ 摘录'} · {p.title || p.filename || '关联内容'}</a>
        {p.quote ? <blockquote>{p.quote}</blockquote> : null}
        {p.locator ? <small>{p.locator}</small> : null}
        {p.review_status ? <small>核验状态：{({ pending: '待核对', accepted: '已核验', rejected: '已拒绝', stale: '需复核' } as Record<string, string>)[p.review_status] || p.review_status}</small> : null}
        {p.effective_archived ? <small>已归档</small> : null}{p.missing ? <small>来源暂不可用</small> : null}
      </div>;
    },
  })();
}
const image = createReactBlockSpec({ type: 'image', content: 'none', propSchema: {
  assetId: { default: '' }, src: { default: '' }, caption: { default: '' }, width: { default: 640 },
} }, { render: ({ block }) => <figure contentEditable={false} className="note-image">
  <img src={block.props.src} alt={block.props.caption || '笔记图片'} style={{ maxWidth: '100%', width: block.props.width }} />
  {block.props.caption ? <figcaption>{block.props.caption}</figcaption> : null}</figure> })();
const schema = BlockNoteSchema.create({ blockSpecs: {
  paragraph: defaultBlockSpecs.paragraph, heading: createHeadingBlockSpec({ allowToggleHeadings: false }),
  bulletListItem: defaultBlockSpecs.bulletListItem, numberedListItem: defaultBlockSpecs.numberedListItem,
  checkListItem: defaultBlockSpecs.checkListItem, quote: defaultBlockSpecs.quote,
  divider: defaultBlockSpecs.divider, image,
  pageRef: reference('pageRef'), documentRef: reference('documentRef'), evidenceRef: reference('evidenceRef'),
} });
const propKeys: Record<string, string[]> = {
  paragraph: ['textColor', 'backgroundColor', 'textAlignment'],
  heading: ['textColor', 'backgroundColor', 'textAlignment', 'level'],
  bulletListItem: ['textColor', 'backgroundColor', 'textAlignment'],
  numberedListItem: ['textColor', 'backgroundColor', 'textAlignment', 'start'],
  checkListItem: ['textColor', 'backgroundColor', 'textAlignment', 'checked'],
  quote: ['textColor', 'backgroundColor', 'textAlignment'], divider: [],
  image: ['assetId', 'src', 'caption', 'width'], pageRef: ['pageId'], documentRef: ['documentId'], evidenceRef: ['evidenceId'],
};
function persistent(blocks: readonly any[]): NoteBlock[] {
  return blocks.map((block) => ({ id: block.id, type: block.type,
    props: Object.fromEntries((propKeys[block.type] || []).filter((key) => block.props[key] !== undefined).map((key) => [key, block.props[key]])),
    content: block.content || [], children: persistent(block.children || []),
  }));
}
function editorBlocks(blocks: NoteBlock[]): any[] {
  return blocks.map((block) => ({ ...block, props: Object.fromEntries(Object.entries(block.props).filter(([key]) =>
    key in (schema.blockSchema as any)[block.type].propSchema)), children: editorBlocks(block.children) }));
}
export default function PageEditor({ body, readonly, onChange, onComposition, onReady }: {
  body: NoteBody; readonly: boolean; onChange: (body: NoteBody) => void;
  onComposition: (active: boolean) => void; onReady: (insert: (block: Partial<NoteBlock>) => void) => void;
}) {
  const editor = useCreateBlockNote({ schema, initialContent: body.blocks.length ? editorBlocks(body.blocks) : undefined });
  const hydrating = useRef(false);
  useEffect(() => {
    hydrating.current = true;
    const refresh = (blocks: NoteBlock[]) => {
      for (const block of blocks) {
        if (['pageRef', 'documentRef', 'evidenceRef', 'image'].includes(block.type)) {
          const current = editor.getBlock(block.id);
          if (current) {
            const allowed = (schema.blockSchema as any)[block.type].propSchema;
            const props = Object.fromEntries(Object.entries(block.props).filter(([key]) => key in allowed));
            if (Object.entries(props).some(([key, value]) => (current.props as any)[key] !== value)) editor.updateBlock(current, { props } as any);
          }
        }
        refresh(block.children);
      }
    };
    try { refresh(body.blocks); } finally { hydrating.current = false; }
  }, [body, editor]);
  const insert = (block: Partial<NoteBlock>) => {
    const cursor = editor.getTextCursorPosition().block;
    editor.insertBlocks([block as any], cursor, 'after');
  };
  // The callback is a ref assignment, so parent updates do not reset the editor.
  onReady(insert);
  return <div className="note-editor" onCompositionStart={() => onComposition(true)} onCompositionEnd={() => onComposition(false)}>
    <BlockNoteView editor={editor} theme="light" editable={!readonly} onChange={() => { if (!hydrating.current) onChange({ schema_version: 1, blocks: persistent(editor.document) }); }} />
  </div>;
}
