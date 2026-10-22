import { useEffect, useMemo, useState } from 'react';
import { createEvidence } from '../api/endpoints';
import type { UnitDetail } from '../api/types';
import { describeError } from '../lib/conflict';
import { quoteContext, relinkDraft, resolveQuoteDraft } from '../lib/excerpt';
import type { QuoteDraft } from '../lib/excerpt';
import { codepointLength, codepointOffsetToUtf16, sliceByCodepoints } from '../lib/unicode';
import { filePageLabel, printedPageLabel, versionLabel } from '../lib/format';
import { Modal } from '../components/Modal';
import { Tag } from '../components/Tag';
import { useAppState } from '../state/AppState';

const CATEGORY_SUGGESTIONS = ['文物建筑', '沿革地理', '碑刻石刻', '民俗风物', '教育制度', '其他'];

interface ExcerptDialogProps {
  open: boolean;
  unit: UnitDetail;
  /** 文献总页数；未知时定位只显示“文件第 N 页”，不猜测总数 */
  pageCount: number | null;
  /** 点击“保存摘录”那一刻算好的草稿（含码点偏移）；没有选区时为 null */
  initialDraft: QuoteDraft | null;
  /** keepDraft 非空表示用户要保留草稿并回到阅读区重新选区 */
  onClose: (keepDraft?: QuoteDraft | null) => void;
  onSaved: () => void;
}

export function ExcerptDialog({ open, unit, pageCount, initialDraft, onClose, onSaved }: ExcerptDialogProps) {
  const { pushToast, bumpDataVersion } = useAppState();
  const [draft, setDraft] = useState<QuoteDraft>(initialDraft ?? { quote: '', startOffset: 0 });
  const [category, setCategory] = useState('');
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<{ kind: 'error' | 'conflict'; message: string } | null>(null);

  // 每次打开都以调用方传来的草稿为准（选区在打开对话框之前就已读取）。
  useEffect(() => {
    if (!open) return;
    setDraft(initialDraft ?? { quote: '', startOffset: 0 });
    setProblem(null);
  }, [initialDraft, open, unit.unit_id]);

  const resolution = useMemo(() => resolveQuoteDraft(unit.text, draft), [unit.text, draft]);
  const context = useMemo(() => quoteContext(unit.text, draft), [unit.text, draft]);
  const occurrenceIndex = resolution.occurrences.indexOf(draft.startOffset);

  const onQuoteChange = (quote: string) => {
    setDraft((current) => relinkDraft(unit.text, { quote, startOffset: current.startOffset }));
    setProblem(null);
  };

  const onSave = async () => {
    const check = resolveQuoteDraft(unit.text, draft);
    if (!check.valid) {
      setProblem({ kind: 'error', message: check.error ?? '引文与原文不一致，请重新选择。' });
      return;
    }
    setSaving(true);
    setProblem(null);
    try {
      await createEvidence({
        unit_id: unit.unit_id,
        quote: draft.quote,
        start_offset: draft.startOffset,
        expected_hash: unit.text_sha256,
        category: category.trim(),
        note: note.trim(),
      });
      pushToast('摘录已保存。', 'info');
      bumpDataVersion();
      onSaved();
      onClose(null);
    } catch (error) {
      const info = describeError(error);
      // 冲突时保留草稿，不清空用户已选文字。
      setProblem({ kind: info.kind === 'version-conflict' ? 'conflict' : 'error', message: info.message });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      title="保存摘录"
      note="引文是该页原文的精确切片；保存后可在摘录集中继续核验与分类。"
      onClose={() => onClose(null)}
      footer={
        <>
          <button type="button" className="btn" onClick={() => onClose(null)}>
            取消
          </button>
          <button type="button" className="btn btn-primary" onClick={() => void onSave()} disabled={saving}>
            {saving ? '保存中…' : '保存摘录'}
          </button>
        </>
      }
    >
      {problem ? (
        <div className={problem.kind === 'conflict' ? 'conflict-box' : 'notice-box'} role="alert">
          {problem.message}
          {problem.kind === 'conflict' ? (
            <div className="btn-row" style={{ marginTop: 8 }}>
              <button type="button" className="btn btn-sm" onClick={() => onClose(draft)}>
                保留草稿，回到阅读区重新选区
              </button>
              <span className="help" style={{ margin: 0 }}>
                草稿不会被清空；也可以直接改上面的引文与出现次序后重试保存。
              </span>
            </div>
          ) : null}
        </div>
      ) : null}

      <section className="field">
        <h3 style={{ fontSize: 13, fontWeight: 500, marginBottom: 6 }}>来源定位</h3>
        <dl className="kv-l">
          <dt>文献</dt>
          <dd>
            {unit.title || unit.filename}
            {unit.title && unit.title !== unit.filename ? <span className="help" style={{ margin: 0 }}>（{unit.filename}）</span> : null}
          </dd>
          <dt>位置</dt>
          <dd>
            {filePageLabel(unit.file_page, pageCount)} · {printedPageLabel(unit.printed_page, unit.printed_page_status)}
          </dd>
          <dt>单元</dt>
          <dd>
            序号 <span className="num">{unit.seq}</span> · 文本版本 {versionLabel(unit.version)} ·{' '}
            <span title={unit.text_sha256}>文本哈希 {unit.text_sha256.slice(0, 10)}…</span>
          </dd>
          {unit.title_path ? (
            <>
              <dt>标题路径</dt>
              <dd>{unit.title_path}</dd>
            </>
          ) : null}
        </dl>
      </section>

      <section className="field">
        <label className="lbl" htmlFor="excerpt-quote">
          引文（逐字原文）
        </label>
        <textarea
          id="excerpt-quote"
          value={draft.quote}
          rows={4}
          onChange={(event) => onQuoteChange(event.target.value)}
          placeholder="在阅读区选中文字后自动填入，也可直接粘贴原文"
          style={{ fontSize: 14 }}
        />
        <div className="btn-row" style={{ marginTop: 6 }}>
          <span className="help" style={{ margin: 0 }}>
            {draft.quote
              ? `共 ${codepointLength(draft.quote)} 字 · 码点偏移 ${draft.startOffset}–${draft.startOffset + codepointLength(draft.quote)} · UTF-16 偏移 ${codepointOffsetToUtf16(unit.text, draft.startOffset)}`
              : '这次没有读到选中文字。可以关闭后在“识别文字”里拖选文字再点保存，也可以直接粘贴原文并选择出现次序。'}
          </span>
        </div>
      </section>

      {resolution.occurrences.length > 1 ? (
        <section className="field">
          <label className="lbl" htmlFor="excerpt-occurrence">
            同一段文字在本页出现 {resolution.occurrences.length} 次，请选择保存哪一次
          </label>
          <select
            id="excerpt-occurrence"
            value={occurrenceIndex >= 0 ? occurrenceIndex : -1}
            onChange={(event) => {
              const index = Number(event.target.value);
              const offset = resolution.occurrences[index];
              if (offset !== undefined) setDraft((current) => ({ ...current, startOffset: offset }));
            }}
          >
            <option value={-1}>当前偏移不匹配，请选择</option>
            {resolution.occurrences.map((offset, index) => (
              <option key={offset} value={index}>
                第 {index + 1} 次出现 · 码点偏移 {offset}
              </option>
            ))}
          </select>
          <p className="help">系统不会用第一次出现代替你选中的那一次。</p>
        </section>
      ) : null}

      {draft.quote ? (
        <section className="field">
          <span className="lbl">原文定位预览</span>
          <div className="quote-box">
            {context.before}
            <mark>{sliceByCodepoints(unit.text, draft.startOffset, draft.startOffset + codepointLength(draft.quote))}</mark>
            {context.after}
          </div>
          <p className="help">高亮部分就是将要保存的引文，取自该偏移处的精确切片。</p>
        </section>
      ) : null}

      <section className="field">
        <label className="lbl" htmlFor="excerpt-category">
          分类
        </label>
        <input
          id="excerpt-category"
          list="excerpt-categories"
          type="text"
          value={category}
          onChange={(event) => setCategory(event.target.value)}
          placeholder="例如：文物建筑"
          style={{ width: '100%' }}
        />
        <datalist id="excerpt-categories">
          {CATEGORY_SUGGESTIONS.map((item) => (
            <option key={item} value={item} />
          ))}
        </datalist>
        <p className="help">分类是自由文本，用于摘录集内筛选。</p>
      </section>

      <section className="field">
        <label className="lbl" htmlFor="excerpt-note">
          备注
        </label>
        <textarea
          id="excerpt-note"
          value={note}
          rows={3}
          onChange={(event) => setNote(event.target.value)}
          placeholder="例如：与扫描页第 775 页核对，原文一致"
        />
        <div className="btn-row" style={{ marginTop: 6 }}>
          <Tag tone="neutral">保存后状态为「待核验」</Tag>
        </div>
      </section>
    </Modal>
  );
}