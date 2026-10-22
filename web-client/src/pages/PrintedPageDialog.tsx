import { useEffect, useState } from 'react';
import { confirmUnitBlank, patchUnitPrintedPage } from '../api/endpoints';
import type { UnitDetail } from '../api/types';
import { describeError } from '../lib/conflict';
import { Modal } from '../components/Modal';
import { printedPageStatusLabel } from '../lib/format';

interface PrintedPageDialogProps {
  unit: UnitDetail;
  onClose: () => void;
  onSaved: (unit: UnitDetail) => void;
}

/**
 * 印刷页码是字符串：可能是 775、罗马数字或中文标注，因此按文本编辑，不做数字解析。
 * 状态由人工标注：候选 / 已确认，不由文件页序自动推算。
 */
export function PrintedPageDialog({ unit, onClose, onSaved }: PrintedPageDialogProps) {
  const [value, setValue] = useState(typeof unit.printed_page === 'string' ? unit.printed_page : '');
  const [status, setStatus] = useState<'candidate' | 'confirmed'>(
    unit.printed_page_status === 'confirmed' ? 'confirmed' : 'candidate',
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setValue(typeof unit.printed_page === 'string' ? unit.printed_page : '');
    setStatus(unit.printed_page_status === 'confirmed' ? 'confirmed' : 'candidate');
    setError(null);
  }, [unit.unit_id, unit.printed_page, unit.printed_page_status]);

  const submit = async () => {
    const trimmed = value.trim();
    if (!trimmed) {
      setError('印刷页码不能为空；本机服务不接受“清空印刷页码”这种请求。');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const updated = await patchUnitPrintedPage(unit.unit_id, { printed_page: trimmed, status });
      onSaved(updated);
      onClose();
    } catch (caught) {
      setError(describeError(caught).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open
      title="编辑印刷页码"
      note="文件页序是文件内的页号，印刷页码是书上印的页码；两者分开记录，识别出的候选页码需要人工确认。"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn btn-primary" onClick={() => void submit()} disabled={saving}>
            {saving ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      {error ? (
        <div className="conflict-box" role="alert">
          {error}
        </div>
      ) : null}
      <dl className="kv-l" style={{ marginBottom: 14 }}>
        <dt>文件页</dt>
        <dd>{unit.file_page === null ? `不适用（Word 文档单元 ${unit.seq}）` : `文件第 ${unit.file_page} 页`}</dd>
        <dt>现值</dt>
        <dd>{typeof unit.printed_page === 'string' && unit.printed_page.trim() ? unit.printed_page : '未填写'}</dd>
        <dt>当前状态</dt>
        <dd>{printedPageStatusLabel(unit.printed_page_status) ?? '未标注'}</dd>
      </dl>
      <div className="field">
        <label className="lbl" htmlFor="printed-page">
          印刷页码（按书上印的样子填写）
        </label>
        <input
          id="printed-page"
          type="text"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="例如 775，也可以写「七七五」或「MCLXXV」"
          style={{ width: '100%' }}
        />
        <p className="help">页码文字按原样保存，不做数字换算，也不从文件页序推断。</p>
      </div>
      <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
        <legend className="lbl">确认程度</legend>
        <label className="radio-row">
          <input type="radio" name="printed-status" checked={status === 'candidate'} onChange={() => setStatus('candidate')} />
          <span>
            候选
            <span className="help" style={{ margin: 0 }}>
              从页眉页脚推测，尚未人工核对。
            </span>
          </span>
        </label>
        <label className="radio-row">
          <input type="radio" name="printed-status" checked={status === 'confirmed'} onChange={() => setStatus('confirmed')} />
          <span>
            已确认
            <span className="help" style={{ margin: 0 }}>
              已对照原页确认。
            </span>
          </span>
        </label>
      </fieldset>
    </Modal>
  );
}

interface ConfirmBlankButtonProps {
  unit: UnitDetail;
  onSaved: (unit: UnitDetail) => void;
}

/**
 * 确认空白页：与本机服务一致——只有「无文字且未完成」的页可以确认。
 * 其它情况按钮禁用并说明原因，不让用户点了才知道不行。
 */
export function ConfirmBlankButton({ unit, onSaved }: ConfirmBlankButtonProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hasText = unit.text.trim().length > 0;
  const eligible = !hasText && unit.status !== 'completed';

  const reason = hasText
    ? '本页已有文字，不能确认为空白。'
    : unit.status === 'completed'
      ? '本页已经完成，不需要确认为空白。'
      : null;

  return (
    <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
      <button
        type="button"
        className="btn btn-sm"
        disabled={!eligible || busy}
        title={reason ?? '确认本页确实没有文字，计入已覆盖页'}
        onClick={async () => {
          setBusy(true);
          setError(null);
          try {
            const updated = await confirmUnitBlank(unit.unit_id);
            onSaved(updated);
          } catch (caught) {
            setError(describeError(caught).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy ? '提交中…' : '确认为空白页'}
      </button>
      {error ? <span className="error-text">{error}</span> : null}
    </span>
  );
}