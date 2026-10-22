import { useEffect, useState } from 'react';
import { patchUnitCorrection } from '../api/endpoints';
import type { UnitDetail } from '../api/types';
import { describeError } from '../lib/conflict';
import type { ErrorInfo } from '../lib/conflict';
import { unitStatusInfo, versionLabel } from '../lib/format';

interface CorrectionEditorProps {
  unit: UnitDetail;
  onSaved: (unit: UnitDetail) => void;
}

/**
 * 人工校订：提交 text + reason + expected_hash。
 * 版本冲突时保留草稿并要求重新核对；原文（original_text）不会被前端改动。
 */
export function CorrectionEditor({ unit, onSaved }: CorrectionEditorProps) {
  const [draft, setDraft] = useState(unit.corrected_text ?? unit.text);
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<ErrorInfo | null>(null);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    setDraft(unit.corrected_text ?? unit.text);
    setReason('');
    setProblem(null);
    setDirty(false);
  }, [unit.unit_id, unit.version, unit.corrected_text, unit.text]);

  const status = unitStatusInfo(unit.status);

  const save = async () => {
    if (saving) return;
    setSaving(true);
    setProblem(null);
    try {
      const updated = await patchUnitCorrection(unit.unit_id, {
        text: draft,
        reason: reason.trim(),
        expected_hash: unit.text_sha256,
      });
      onSaved(updated);
    } catch (error) {
      setProblem(describeError(error));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="note-box">
      <h4>人工校订</h4>
      {problem ? (
        <div className={problem.kind === 'version-conflict' ? 'conflict-box' : 'notice-box'} role="alert">
          {problem.message}
          {problem.kind === 'version-conflict' ? (
            <div className="btn-row" style={{ marginTop: 8 }}>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => {
                  setDraft(unit.corrected_text ?? unit.text);
                  setReason('');
                  setDirty(false);
                }}
              >
                放弃草稿并载入当前文字
              </button>
              <span className="help" style={{ margin: 0 }}>
                也可以先对照原页，再决定保留草稿还是重载。
              </span>
            </div>
          ) : null}
        </div>
      ) : null}

      <p className="help" style={{ marginTop: 0 }}>
        当前文本取自{versionLabel(unit.version)} · 状态 {status.label}。保存会按当前文本哈希校验版本，原文不会被前端改写。
      </p>
      <label className="lbl" htmlFor="correction-text">
        校订后的文字
      </label>
      <textarea
        id="correction-text"
        value={draft}
        rows={8}
        onChange={(event) => {
          setDraft(event.target.value);
          setDirty(true);
        }}
        style={{ fontFamily: 'inherit', fontSize: 14 }}
      />
      <label className="lbl" htmlFor="correction-reason" style={{ marginTop: 10 }}>
        校订原因（必填，写清依据）
      </label>
      <input
        id="correction-reason"
        type="text"
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        placeholder="例如：对照扫描页，「花并」实为「花井」"
        style={{ width: '100%' }}
      />
      <div className="btn-row" style={{ marginTop: 8 }}>
        <button type="button" className="btn btn-sm btn-primary" onClick={() => void save()} disabled={saving || !dirty || !reason.trim()}>
          {saving ? '保存中…' : '保存校订'}
        </button>
        <button
          type="button"
          className="btn btn-sm"
          disabled={!dirty}
          onClick={() => {
            setDraft(unit.corrected_text ?? unit.text);
            setDirty(false);
          }}
        >
          放弃改动
        </button>
        <span className="help" style={{ margin: 0 }}>
          保存后本页摘录会按版本变化标记为需复核。
        </span>
      </div>
    </div>
  );
}