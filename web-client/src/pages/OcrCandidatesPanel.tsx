/**
 * 阅读器「对照」里的候选控件：原图在上方保持不变，这里只负责现用 / 候选 / 旧 OCR 的
 * 文字对照与采用/保留决定。
 *
 * 关键约束：
 * - 候选与旧 OCR 的文字用独立元素渲染，不带正式摘录的 `.unit-text`/textRef；
 *   阅读器的全局「保存摘录」在候选/旧 OCR 显示时必须禁用（由 onViewChange 通知）。
 * - 列表只给候选摘要（可省略 result/error）：选中后按需 GET /ocr-candidates/{id} 取完整详情；
 *   响应已含完整结果时直接复用，不再多发请求。未取得详情前不把空白占位当作可采用的候选。
 * - 按 unit_id 隔离请求；切换页立即清除本页候选与决定，迟到响应不串页。
 * - 只有 ready、已取得完整结果且 base_content_revision 等于当前页修订号才允许采用；服务端最终裁决。
 * - 首次生成可能还没有候选行：传入 jobId 时轮询任务与候选列表，直到出现候选或任务结束。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode, Ref } from 'react';
import { adoptOcrCandidate, getJob, getOcrCandidate, listJobUnits, listOcrCandidates, rejectOcrCandidate } from '../api/endpoints';
import type { JobItem, OcrCandidateDetail, OcrCandidateSummary, Page, UnitDetail } from '../api/types';
import { useApiQuery } from '../hooks/useApiQuery';
import { describeError } from '../lib/conflict';
import { diffText } from '../lib/ocrCandidates';
import type { DiffResult, DiffSpan } from '../lib/ocrCandidates';
import { formatTime, isActiveJob } from '../lib/format';
import { Tag } from '../components/Tag';

export type OcrCandidateView = 'current' | 'candidate' | 'old';

interface OcrCandidatesPanelProps {
  unit: UnitDetail;
  onSaved: () => void;
  onSelectUnit?: (unitId: string) => void;
  jobId?: string;
  /** 用于让阅读器在候选/旧 OCR 显示时禁用全局「保存摘录」。 */
  onViewChange?: (view: OcrCandidateView) => void;
  currentTextRef?: Ref<HTMLParagraphElement>;
}

const STATE_LABEL: Record<string, { label: string; tone: 'neutral' | 'info' | 'warn' | 'error' | 'progress' }> = {
  queued: { label: '排队中', tone: 'neutral' },
  running: { label: '识别中', tone: 'progress' },
  ready: { label: '待核对', tone: 'info' },
  failed: { label: '失败', tone: 'error' },
  adopted: { label: '已采用', tone: 'info' },
  rejected: { label: '已保留', tone: 'warn' },
  stale: { label: '已陈旧', tone: 'warn' },
};

function stateInfo(state: string): { label: string; tone: 'neutral' | 'info' | 'warn' | 'error' | 'progress' } {
  return STATE_LABEL[state] ?? { label: state, tone: 'neutral' };
}

function renderMarked(
  segments: DiffSpan[],
  side: 'current' | 'candidate',
  fallback: string,
  diffOn: boolean,
  bounded: boolean,
): ReactNode {
  if (!diffOn || bounded) return fallback;
  const nodes: ReactNode[] = [];
  segments.forEach((span, index) => {
    if (span.kind === 'equal') {
      const text = side === 'candidate' ? span.candidate : span.current;
      if (text) nodes.push(<span key={index}>{text}</span>);
      return;
    }
    const text = side === 'candidate' ? span.candidate : span.current;
    if (!text) return;
    nodes.push(
      <mark key={index} className="ocp-diff">
        {text}
      </mark>,
    );
  });
  return nodes;
}

export function OcrCandidatesPanel({ unit, onSaved, onSelectUnit, jobId, onViewChange, currentTextRef }: OcrCandidatesPanelProps) {
  const [view, setView] = useState<OcrCandidateView>('candidate');
  const [diffOn, setDiffOn] = useState(false);
  const [decided, setDecided] = useState<OcrCandidateDetail | null>(null);
  const [authoritative, setAuthoritative] = useState<UnitDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pollList, setPollList] = useState(false);
  const [jobStopped, setJobStopped] = useState(false);

  const viewChangeRef = useRef(onViewChange);
  viewChangeRef.current = onViewChange;
  const canSelectUnit = Boolean(onSelectUnit);

  // 采用/保留可能在切页后才返回：用「单调递增的单元 epoch + 挂载状态」拦截。
  // 用 epoch 而非 unit_id，是为了 A→B→A 时同一个 unit_id 也不能让第一个 A 的迟到结果写回。
  const unitEpochRef = useRef(0);
  const scope = `${unit.unit_id}:${jobId ?? ''}`;
  const prevScopeRef = useRef(scope);
  if (prevScopeRef.current !== scope) {
    prevScopeRef.current = scope;
    unitEpochRef.current += 1;
  }
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // 通知阅读器当前对照侧；离开候选/旧 OCR 或卸载时恢复为现用。
  useEffect(() => {
    viewChangeRef.current?.(view);
  }, [view]);
  useEffect(() => () => viewChangeRef.current?.('current'), []);

  // 切页立即清除本页的决定、忙碌与错误，避免上一页的内容带过来。
  useEffect(() => {
    setDecided(null);
    setAuthoritative(null);
    setError(null);
    setBusy(false);
    setView('candidate');
  }, [unit.unit_id, jobId]);

  const effectiveUnit = authoritative?.unit_id === unit.unit_id &&
    (authoritative.content_revision ?? -1) >= (unit.content_revision ?? -1)
      ? authoritative : unit;

  const listQuery = useApiQuery<Page<OcrCandidateSummary>>(
    `ocr-candidates:${unit.unit_id}:${jobId ?? ''}`,
    (signal) => listOcrCandidates(unit.unit_id, { page: 1, pageSize: 30, jobId }, signal),
    { poll: pollList },
  );

  // listQuery.stale 表示手上的行来自上一个 unit/key：忽略它，避免切页瞬间据旧行显示候选或发详情请求。
  const listRow = listQuery.stale ? null : listQuery.data?.items?.[0] ?? null;
  // decided 属于某个具体单元；切页后即使旧请求迟到写入也按 unit_id 拦截。
  const decidedForUnit = decided && decided.unit_id === unit.unit_id &&
    (!jobId || decided.job_id === jobId) ? decided : null;
  const summaryRow = decidedForUnit ?? listRow;
  const summary = summaryRow && summaryRow.unit_id === unit.unit_id ? summaryRow : null;
  const summaryActive = summary?.state === 'queued' || summary?.state === 'running';

  // 列表只给摘要（省略 result/error）时按需取详情；响应已含完整结果就直接用，不再多发请求。
  const needsDetail = summary !== null && summary.result === undefined;

  // 导航用的任务：优先显式 jobId（刚新建任务），否则从候选行反推 job_id。
  // 只在提供 onSelectUnit（任务页/阅读器的对照流）时反推，避免只读用法平白多发任务请求。
  const derivedJobId = canSelectUnit ? summary?.job_id ?? undefined : undefined;
  const navigationJobId = jobId ?? derivedJobId;

  useEffect(() => {
    setJobStopped(false);
  }, [navigationJobId]);

  const jobQuery = useApiQuery<JobItem>(
    navigationJobId ? `ocr-candidate-job:${navigationJobId}` : 'ocr-candidate-job:none',
    (signal) => getJob(navigationJobId as string, signal),
    { enabled: Boolean(navigationJobId), poll: Boolean(navigationJobId) && !jobStopped },
  );
  const job = jobQuery.stale ? undefined : jobQuery.data ?? undefined;
  const jobTerminal = job ? !isActiveJob(job.state) : false;

  // 详情键带上候选状态与基础修订号：摘要 running→ready（或重跑换修订号）时重新取详情。
  // 否则会一直沿用第一次取回的 running/result=null 详情，ready 的候选文字永远显示不出来。
  // stale 的详情（属于上一个 key）一并忽略。
  const detailQuery = useApiQuery<OcrCandidateDetail>(
    summary ? `ocr-candidate:${summary.id}:${summary.state}:${summary.base_content_revision}` : 'ocr-candidate:none',
    (signal) => getOcrCandidate(summary!.id, signal),
    { enabled: needsDetail },
  );
  const loadedDetail =
    detailQuery.data && !detailQuery.stale && summary && detailQuery.data.id === summary.id
      ? detailQuery.data
      : null;
  const candidate: OcrCandidateSummary | null = decidedForUnit ?? loadedDetail ?? summary;
  const candidatePayloadKnown = candidate !== null && candidate.result !== undefined;

  const reloadList = listQuery.reload;

  // 任务未结束时：还没有可用候选就持续轮询列表，任务一结束就停止并做一次最终刷新捕捉最后一个候选。
  // 已出现候选但仍未结束：停列表轮询，但继续轮询任务本身，好在任务完成时刷新「下一待核页」。
  useEffect(() => {
    if (!navigationJobId) {
      setPollList(summaryActive);
      return;
    }
    if (jobTerminal) {
      setPollList(false);
      setJobStopped(true);
      if (!summary || summaryActive) reloadList();
      return;
    }
    if (!summary || summaryActive) {
      setPollList(true);
      return;
    }
    setPollList(false);
  }, [summaryActive, summary, navigationJobId, jobTerminal, reloadList]);

  // 同一任务内查找其它待核对页（排除当前页），用分页的 job units 扫描，供「下一待核页」导航。
  // jobTerminal 与 summary.state 变化（任务完成、候选就绪、采用/保留决定）都会重新扫描，
  // 而不是任务排队时只查一次——那时各页的候选可能都还没生成。
  const [nextPendingUnitId, setNextPendingUnitId] = useState<string | null>(null);
  const [navigationError, setNavigationError] = useState<string | null>(null);
  const [navigationLoading, setNavigationLoading] = useState(false);
  const [navigationRefresh, setNavigationRefresh] = useState(0);
  useEffect(() => {
    if (!navigationJobId || !canSelectUnit) {
      setNextPendingUnitId(null);
      setNavigationLoading(false);
      setNavigationError(null);
      return;
    }
    setNextPendingUnitId(null);
    setNavigationLoading(true);
    setNavigationError(null);
    const controller = new AbortController();
    let alive = true;
    void (async () => {
      try {
        const pageSize = 100;
        let page = 1;
        let found: string | null = null;
        for (let guard = 0; guard < 20; guard += 1) {
          const response = await listJobUnits(navigationJobId, page, pageSize, controller.signal);
          const ready = response.items.find(
            (item) => item.candidate_state === 'ready' && item.unit_id !== unit.unit_id,
          );
          if (ready) {
            found = ready.unit_id;
            break;
          }
          const size = response.page_size || pageSize;
          const pages = size > 0 ? Math.ceil(response.total / size) : 1;
          if (page >= pages) break;
          page += 1;
        }
        if (alive) setNextPendingUnitId(found);
      } catch (caught) {
        if (alive) setNavigationError(describeError(caught).message);
      } finally {
        if (alive) setNavigationLoading(false);
      }
    })();
    return () => {
      alive = false;
      controller.abort();
      setNextPendingUnitId(null);
    };
  }, [navigationJobId, canSelectUnit, unit.unit_id, jobTerminal, summary?.state, navigationRefresh]);

  const candidateText = candidate?.result?.text ?? '';
  const baseText = view === 'old' ? (effectiveUnit.original_text ?? '') : effectiveUnit.text;
  const diff: DiffResult = useMemo(() => diffText(baseText, candidateText), [baseText, candidateText]);

  const currentRevision = effectiveUnit.content_revision;
  const revisionMatches =
    typeof currentRevision === 'number' && candidate !== null && candidate.base_content_revision === currentRevision;
  const canAdopt = candidate?.state === 'ready' && candidatePayloadKnown && revisionMatches && !busy;
  const canReject =
    candidate !== null && (candidate.state === 'ready' || candidate.state === 'failed' || candidate.state === 'stale') && !busy;

  // 采用/保留返回时若单元已切换（含 A→B→A）或组件已卸载，丢弃结果，绝不写回或触发 onSaved。
  const isCurrentUnit = (startEpoch: number) => mountedRef.current && unitEpochRef.current === startEpoch;

  const adopt = async () => {
    if (!candidate || !canAdopt || typeof currentRevision !== 'number') return;
    const startEpoch = unitEpochRef.current;
    setBusy(true);
    setError(null);
    try {
      const result = await adoptOcrCandidate(candidate.id, { expected_content_revision: currentRevision });
      if (!isCurrentUnit(startEpoch)) return;
      setDecided(result.candidate);
      setAuthoritative(result.unit);
      setView('current');
      onSaved();
    } catch (caught) {
      if (!isCurrentUnit(startEpoch)) return;
      setError(describeError(caught).message);
    } finally {
      if (isCurrentUnit(startEpoch)) setBusy(false);
    }
  };

  const reject = async () => {
    if (!candidate || !canReject) return;
    const startEpoch = unitEpochRef.current;
    setBusy(true);
    setError(null);
    try {
      const updated = await rejectOcrCandidate(candidate.id, { reason: '' });
      if (!isCurrentUnit(startEpoch)) return;
      setDecided(updated);
      setView('current');
    } catch (caught) {
      if (!isCurrentUnit(startEpoch)) return;
      setError(describeError(caught).message);
    } finally {
      if (isCurrentUnit(startEpoch)) setBusy(false);
    }
  };

  const status = buildStatus({
    candidate,
    candidatePayloadKnown,
    job,
    jobTerminal,
    jobId,
    currentRevision,
    revisionMatches,
  });

  return (
    <section className="ocr-candidate-panel" aria-label="识别候选对照">
      <div className="ocp-tabs" role="tablist" aria-label="对照文字版本">
        <button
          type="button"
          role="tab"
          aria-selected={view === 'current'}
          className="ocp-tab"
          onClick={() => setView('current')}
        >
          现用文字
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === 'candidate'}
          className="ocp-tab"
          onClick={() => setView('candidate')}
        >
          候选文字
        </button>
        <button type="button" role="tab" aria-selected={view === 'old'} className="ocp-tab" onClick={() => setView('old')}>
          旧 OCR 底稿
        </button>
      </div>

      <div className="ocp-diff-bar">
        <label className="ocp-toggle">
          <input type="checkbox" checked={diffOn} onChange={(event) => setDiffOn(event.target.checked)} />
          <span>标出差异</span>
        </label>
        {candidate ? (
          <Tag tone={stateInfo(candidate.state).tone} state>
            {stateInfo(candidate.state).label}
          </Tag>
        ) : null}
        {diffOn ? <span className="ocp-note">差异只表示不同，不代表更正确。</span> : null}
        {diffOn && diff.bounded ? (
          <span className="ocp-note">文字较多，已改为并排全文对照（内容未截断）。</span>
        ) : null}
      </div>

      <div className="ocp-text" role="tabpanel">
        {view === 'current' ? (
          <p ref={currentTextRef} className="ocr-text unit-text" lang="zh-CN">{renderMarked(diff.spans, 'current', effectiveUnit.text, diffOn, diff.bounded)}</p>
        ) : null}
        {view === 'old' ? (
          effectiveUnit.original_text ? (
            <p className="ocr-text">{renderMarked(diff.spans, 'current', effectiveUnit.original_text, diffOn, diff.bounded)}</p>
          ) : (
            <p className="ocp-empty">本页没有留下旧的机器 OCR 底稿。</p>
          )
        ) : null}
        {view === 'candidate' ? (
          candidateText ? (
            <p className="ocr-text">{renderMarked(diff.spans, 'candidate', candidateText, diffOn, diff.bounded)}</p>
          ) : (
            <p className="ocp-empty">
              {emptyCandidateHint({
                candidate,
                job,
                jobTerminal,
                jobId,
                detailLoading: detailQuery.loading,
                detailError: detailQuery.error?.message ?? null,
              })}
            </p>
          )
        ) : null}
      </div>

      {error ? (
        <div className="conflict-box" role="alert" style={{ margin: '0 12px 10px' }}>
          {error}
        </div>
      ) : null}

      {listQuery.error || jobQuery.error || (needsDetail && detailQuery.error) ? (
        <div className="conflict-box" role="alert" style={{ margin: '0 12px 10px' }}>
          无法读取候选或任务：{listQuery.error?.message ?? jobQuery.error?.message ?? detailQuery.error?.message}
          <button type="button" className="btn btn-sm" onClick={() => {
            listQuery.reload();
            if (navigationJobId) jobQuery.reload();
            if (needsDetail) detailQuery.reload();
          }}>重试读取</button>
        </div>
      ) : null}

      <div className="ocp-status" role="status" aria-live="polite">
        {status}
      </div>

      <div className="ocp-actions">
        <button type="button" className="btn btn-sm btn-primary" disabled={!canAdopt} onClick={() => void adopt()}>
          采用候选
        </button>
        <button type="button" className="btn btn-sm" disabled={!canReject} onClick={() => void reject()}>
          保留现用
        </button>
        {typeof currentRevision === 'number' ? (
          <span className="ocp-note">当前页修订号 {currentRevision}</span>
        ) : (
          <span className="ocp-note">未记录当前页修订号</span>
        )}
      </div>

      {onSelectUnit ? (
        <div className="ocp-nav">
          <button
            type="button"
            className="btn btn-sm"
            disabled={!nextPendingUnitId}
            onClick={() => {
              if (nextPendingUnitId) onSelectUnit(nextPendingUnitId);
            }}
          >
            下一待核页
          </button>
          <span className="ocp-note">
            {nextPendingUnitId ? '跳到同一任务内其它待核对页。' : navigationLoading ? '正在读取待核页…' : navigationError ? '待核页读取失败。' : '本任务内暂无其它待核对页。'}
          </span>
          {navigationError ? (
            <div className="error-text" role="alert">{navigationError}
              <button type="button" className="btn btn-sm" onClick={() => setNavigationRefresh(n => n + 1)}>重试待核页</button>
            </div>
          ) : null}
        </div>
      ) : null}

      {candidate ? (
        <details className="ocp-details">
          <summary>识别详情</summary>
          <dl className="ocp-detail-list">
            <dt>配置</dt>
            <dd>{candidate.profile_id}</dd>
            <dt>布局</dt>
            <dd>{candidate.layout}</dd>
            <dt>引擎</dt>
            <dd>{candidate.result?.engine ?? '未记录'}</dd>
            <dt>渲染尺寸</dt>
            <dd>
              {candidate.result?.render_width ?? '—'} × {candidate.result?.render_height ?? '—'}
            </dd>
            <dt>创建时间</dt>
            <dd>{formatTime(candidate.created_at)}</dd>
            {candidate.error ? (
              <>
                <dt>错误</dt>
                <dd>
                  {candidate.error.kind} · {candidate.error.message}
                </dd>
              </>
            ) : null}
          </dl>
          {candidate.result ? (
            <>
              <h4>模型指纹</h4>
              <pre className="ocp-json">{JSON.stringify(candidate.result.model_fingerprints ?? null, null, 2)}</pre>
              <h4>实际识别参数</h4>
              <pre className="ocp-json">{JSON.stringify(candidate.result.config ?? null, null, 2)}</pre>
              <h4>耗时与资源采样</h4>
              <p className="ocp-note">记录来自本次识别；采样峰值不代表精确系统峰值。未记录的数值显示为 null。</p>
              <pre className="ocp-json">{JSON.stringify({ timing: candidate.result.timing ?? null, memory: candidate.result.memory ?? null }, null, 2)}</pre>
            </>
          ) : null}
        </details>
      ) : null}
    </section>
  );
}

function emptyCandidateHint(input: {
  candidate: OcrCandidateSummary | null;
  job: JobItem | undefined;
  jobTerminal: boolean;
  jobId: string | undefined;
  detailLoading: boolean;
  detailError: string | null;
}): string {
  const { candidate, job, jobTerminal, jobId, detailLoading, detailError } = input;
  // 列表只给摘要：详情未到之前不能把空白占位当成完整候选。
  if (candidate && candidate.result === undefined) {
    if (detailLoading) return '正在读取候选详情……取得完整结果后才能采用。';
    if (detailError) return `候选详情读取失败：${detailError}`;
    return '候选详情尚未取得，暂不能采用。';
  }
  if (candidate?.state === 'running') return '正在生成候选……识别完成后在此显示候选文字供对照。';
  if (candidate?.state === 'queued') return '候选排队中……完成后在此显示候选文字。';
  if (candidate?.state === 'failed') return '本页识别失败，旧文字仍可用。可在识别详情查看原因。';
  if (jobId && job && !jobTerminal) return '任务进行中：本页候选生成后会显示在这里。';
  if (jobId && jobTerminal) return '本次任务没有为当前页产生候选（范围可能不包含此页）。';
  return '本页还没有候选。可在上方「生成识别候选」按范围生成。';
}

function buildStatus(input: {
  candidate: OcrCandidateSummary | null;
  candidatePayloadKnown: boolean;
  job: JobItem | undefined;
  jobTerminal: boolean;
  jobId: string | undefined;
  currentRevision: number | undefined;
  revisionMatches: boolean;
}): string {
  const { candidate, candidatePayloadKnown, job, jobTerminal, jobId, currentRevision, revisionMatches } = input;
  if (!candidate) {
    if (jobId && job && !jobTerminal) return '正在为该页生成候选……识别完成前采用操作不可用。';
    if (jobId && jobTerminal) return '本次任务没有为当前页产生候选；未采用任何内容。';
    return '本页还没有候选，未采用任何内容。';
  }
  switch (candidate.state) {
    case 'queued':
      return '候选排队中……采用操作暂不可用。';
    case 'running':
      return '正在生成候选……识别完成前采用操作不可用。';
    case 'failed':
      return `本页识别失败：${candidate.error?.message ?? '原因未记录'}。旧文字仍可用，失败页不产生待核对候选。`;
    case 'stale':
      return '候选基于旧版本，已标记为陈旧；请重新核对或重新生成，不提供无检查的强行覆盖。';
    case 'adopted':
      return '已采用候选：仅更新机器识别底稿，人工校订仍作为现用文本用于检索与摘录。';
    case 'rejected':
      return '已保留现用文字：候选记录保留但不采用，现用文字、检索与摘录不变。';
    case 'ready':
      if (!candidatePayloadKnown) {
        return '候选已就绪，正在读取完整结果；取得详情前采用操作不可用。';
      }
      if (typeof currentRevision !== 'number') {
        return '无法确认当前页修订号，暂不能采用；请刷新后重新核对。';
      }
      if (!revisionMatches) {
        return `候选基于旧版本（基础修订号 ${candidate.base_content_revision}，当前 ${currentRevision}），现用文字已变化，请重新核对。`;
      }
      return '候选已就绪，可对照后决定。差异只表示不同，不代表更正确。';
    default:
      return '候选状态未知，暂不能采用。';
  }
}
