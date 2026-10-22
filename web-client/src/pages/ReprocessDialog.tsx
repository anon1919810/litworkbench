/**
 * 生成识别候选对话框（Task C）。
 *
 * 与旧「强制重新识别」的区别：这里只排队候选任务，不直接改写现用文字；候选完成后
 * 在阅读器「对照」里逐页人工决定采用或保留。范围由全部 unit 摘要解析成显式 unit_ids
 * （文件页去重、不把印刷页当文件页、上限 1000）。props 保持 {unit, onClose, onQueued}。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { createOcrCandidateJob, listAllUnits, listOcrProfiles } from '../api/endpoints';
import type { JobItem, OcrProfile, OcrProfileList, UnitDetail, UnitSummary } from '../api/types';
import { describeError } from '../lib/conflict';
import { resolveUnitRange } from '../lib/ocrCandidates';
import type { RangeMode } from '../lib/ocrCandidates';
import { useApiQuery } from '../hooks/useApiQuery';
import { Modal } from '../components/Modal';

type Scope = RangeMode;

interface ReprocessDialogProps {
  unit: UnitDetail;
  onClose: () => void;
  /** 第二个参数可选：调用方可以据此轮询该任务的候选生成进度。 */
  onQueued: (message: string, job?: JobItem) => void;
}

function toInt(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isInteger(parsed) ? parsed : null;
}

/** 有省资源配置且可用时优先选它；否则回退到可用项；都不可用则不默认选择。 */
function pickDefaultProfile(profiles: OcrProfile[]): string | null {
  const available = profiles.filter((profile) => profile.available);
  const preferred =
    available.find((profile) => profile.id === 'small-lowmem-v1') ??
    available.find((profile) => profile.id === 'compatible-v1') ??
    available[0];
  return preferred ? preferred.id : null;
}

export function ReprocessDialog({ unit, onClose, onQueued }: ReprocessDialogProps) {
  const [scope, setScope] = useState<Scope>('page');
  const [profileId, setProfileId] = useState<string | null>(null);
  const [rangeFrom, setRangeFrom] = useState(String(unit.file_page ?? 1));
  const [rangeTo, setRangeTo] = useState(String(unit.file_page ?? 1));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  const unitScope = useRef(unit.unit_id);
  unitScope.current = unit.unit_id;
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const close = () => {
    mounted.current = false;
    onClose();
  };

  const profilesQuery = useApiQuery<OcrProfileList>('ocr-profiles', (signal) => listOcrProfiles(signal));
  const profiles = profilesQuery.data?.items ?? [];
  const effectiveProfileId = profileId ?? pickDefaultProfile(profiles);

  const needsUnits = scope !== 'page';
  const unitsQuery = useApiQuery<UnitSummary[]>(
    needsUnits ? `ocr-doc-units:${unit.doc_id}` : 'ocr-doc-units:none',
    (signal) => listAllUnits(unit.doc_id, signal),
    { enabled: needsUnits },
  );

  const resolution = useMemo(() => {
    const units = (unitsQuery.data ?? []).map((item) => ({ id: item.id, file_page: item.file_page, kind: item.kind }));
    if (scope === 'page') {
      // 单页也要带上当前单元，才能按文件页判断 Word（页范围只取当前页，不必取整本）。
      return resolveUnitRange([{ id: unit.unit_id, file_page: unit.file_page, kind: unit.kind }], 'page', {
        currentUnitId: unit.unit_id,
      });
    }
    if (scope === 'document') return resolveUnitRange(units, 'document');
    return resolveUnitRange(units, 'range', { from: toInt(rangeFrom), to: toInt(rangeTo) });
  }, [unitsQuery.data, scope, rangeFrom, rangeTo, unit.unit_id, unit.file_page, unit.kind]);
  const scopeBlocked = needsUnits && (unitsQuery.loading || Boolean(unitsQuery.error) || unitsQuery.stale);

  const submit = async () => {
    if (resolution.error || !effectiveProfileId || busy || scopeBlocked) return;
    const submittedUnit = unit.unit_id;
    const current = () => mounted.current && unitScope.current === submittedUnit;
    setBusy(true);
    setError(null);
    try {
      const job = await createOcrCandidateJob(unit.doc_id, {
        unit_ids: resolution.unitIds,
        profile_id: effectiveProfileId,
        layout: 'original',
      });
      if (!current()) return;
      onQueued(
        `已提交 ${resolution.count} 页的识别候选任务，可在识别任务页查看进度；完成后逐页「待核对」，采用前现用文字不变。`,
        job,
      );
      onClose();
    } catch (caught) {
      if (current()) setError(describeError(caught).message);
    } finally {
      if (current()) setBusy(false);
    }
  };

  return (
    <Modal
      open
      title="生成识别候选"
      note="候选是独立任务：只排队识别、不改写现用文字。完成后在阅读器「对照」里逐页人工决定采用或保留。"
      onClose={close}
      footer={
        <>
          <button type="button" className="btn" onClick={close}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void submit()}
            disabled={busy || Boolean(resolution.error) || !effectiveProfileId || scopeBlocked}
          >
            {busy ? '提交中…' : '生成候选'}
          </button>
        </>
      }
    >
      {error ? (
        <div className="conflict-box" role="alert">
          {error}
        </div>
      ) : null}
      <dl className="kv-l" style={{ marginBottom: 12 }}>
        <dt>文献</dt>
        <dd>{unit.title || unit.filename}</dd>
        <dt>当前页</dt>
        <dd>
          {unit.file_page === null ? `单元 ${unit.seq}` : `文件第 ${unit.file_page} 页`}
          {unit.printed_page ? ` · 印刷页码 ${unit.printed_page}` : ''}
        </dd>
      </dl>

      <h4 style={{ fontSize: 12.5, color: 'var(--text-2)', margin: '0 0 6px' }}>范围</h4>
      <label className="radio-row">
        <input
          type="radio"
          name="ocr-scope"
          value="page"
          checked={scope === 'page'}
          onChange={() => setScope('page')}
        />
        <span>
          只处理当前页
          <span className="help" style={{ margin: 0 }}>
            只重新识别这一页，适合识别明显错误的单页。
          </span>
        </span>
      </label>
      <label className="radio-row">
        <input
          type="radio"
          name="ocr-scope"
          value="range"
          checked={scope === 'range'}
          onChange={() => setScope('range')}
        />
        <span>
          指定文件页范围
          <span className="help" style={{ margin: 0 }}>
            按文件页顺序处理；印刷页码不参与匹配。
          </span>
        </span>
      </label>
      {scope === 'range' ? (
        <div className="btn-row" style={{ margin: '4px 0 8px' }}>
          <label htmlFor="ocr-range-from" style={{ fontSize: 12.5, color: 'var(--text-2)' }}>
            起始文件页
          </label>
          <input
            id="ocr-range-from"
            type="number"
            min={1}
            value={rangeFrom}
            onChange={(event) => setRangeFrom(event.target.value)}
            style={{ width: 80 }}
          />
          <label htmlFor="ocr-range-to" style={{ fontSize: 12.5, color: 'var(--text-2)' }}>
            结束文件页
          </label>
          <input
            id="ocr-range-to"
            type="number"
            min={1}
            value={rangeTo}
            onChange={(event) => setRangeTo(event.target.value)}
            style={{ width: 80 }}
          />
        </div>
      ) : null}
      <label className="radio-row">
        <input
          type="radio"
          name="ocr-scope"
          value="document"
          checked={scope === 'document'}
          onChange={() => setScope('document')}
        />
        <span>
          整本文献
          <span className="help" style={{ margin: 0 }}>
            逐页排队；页数很多时耗时较长。
          </span>
        </span>
      </label>

      {needsUnits && unitsQuery.loading ? <p className="fine-print">正在读取文件页列表…</p> : null}
      {needsUnits && unitsQuery.error ? (
        <div className="conflict-box" role="alert">
          无法读取文件页列表：{unitsQuery.error.message}
          <button type="button" className="btn btn-sm" onClick={unitsQuery.reload}>重试页列表</button>
        </div>
      ) : null}
      {scopeBlocked ? null : resolution.error ? (
        <div className="conflict-box" role="alert" style={{ margin: '8px 0' }}>
          {resolution.error}
        </div>
      ) : (
        <p className="fine-print" style={{ marginTop: 8 }}>
          将生成 {resolution.count} 页候选
          {needsUnits && resolution.knownMaxPage !== null ? `（已载入页摘要，最大文件页序 ${resolution.knownMaxPage}）` : ''}。
        </p>
      )}

      <h4 style={{ fontSize: 12.5, color: 'var(--text-2)', margin: '14px 0 6px' }}>识别配置</h4>
      {profilesQuery.loading ? <p className="fine-print">正在读取可用配置…</p> : null}
      {profilesQuery.error ? <p className="error-text">{profilesQuery.error.message}</p> : null}
      {profiles.map((profile) => (
        <label className="radio-row" key={profile.id}>
          <input
            type="radio"
            name="ocr-profile"
            value={profile.id}
            checked={effectiveProfileId === profile.id}
            disabled={!profile.available}
            onChange={() => setProfileId(profile.id)}
          />
          <span>
            {profile.label || profile.id}
            {profile.description ? (
              <span className="help" style={{ margin: 0 }}>
                {profile.description}
              </span>
            ) : null}
            {!profile.available && profile.reason ? (
              <span className="help" style={{ margin: 0, color: 'var(--red-text)' }}>
                {profile.reason}
              </span>
            ) : null}
          </span>
        </label>
      ))}

      <p className="fine-print">
        范围外的页不会被改动。采用候选只更新机器识别底稿；已保存的人工校订与旧摘录继续保持，并在需要时提示复核。
      </p>
    </Modal>
  );
}
