import { useState } from 'react';
import type { DocumentItem, JobItem, JobUnitItem } from '../api/types';
import type { Page } from '../api/types';
import { getDocument, listJobUnits, pauseJob, resumeJob, retryJobFailed } from '../api/endpoints';
import { IconJobs } from '../components/Icons';
import { PageHeader } from '../components/PageHeader';
import { Tag } from '../components/Tag';
import { useApiQuery } from '../hooks/useApiQuery';
import { useJobs } from '../hooks/useLibraryData';
import { usePolling } from '../hooks/usePolling';
import { describeError } from '../lib/conflict';
import { formatTime, isActiveJob, jobStateInfo, jobUnitLabel, jobUnitOpenable, jobUnitStateInfo, methodLabel, num } from '../lib/format';
import { useAppState } from '../state/AppState';

type JobAction = 'pause' | 'resume' | 'retry';

/** 候选任务逐页 item 上可选的候选状态词；未收录值原样显示。 */
function candidateStateLabel(state: string): string {
  const known: Record<string, string> = {
    queued: '候选排队中',
    running: '候选识别中',
    ready: '待核对候选',
    failed: '候选失败',
    adopted: '候选已采用',
    rejected: '候选已保留',
    stale: '候选已陈旧',
  };
  return known[state] ?? state;
}

export function JobsPage() {
  const { openReader, refreshCounts, pushToast } = useAppState();
  const [page, setPage] = useState(1);
  const jobs = useJobs(page);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  usePolling(jobs.reload, { enabled: jobs.activeCount > 0, foregroundMs: 2_000, backgroundMs: 10_000 });

  const items = jobs.data?.items ?? [];
  const pages = jobs.data ? Math.max(1, Math.ceil(jobs.data.total / Math.max(1, jobs.data.page_size))) : 1;

  const run = async (job: JobItem, action: JobAction) => {
    const key = `${job.id}:${action}`;
    if (busy) return;
    setBusy(key);
    setError(null);
    try {
      const call = action === 'pause' ? pauseJob : action === 'resume' ? resumeJob : retryJobFailed;
      const updated = await call(job.id);
      const label =
        action === 'pause'
          ? '已请求暂停（当前页完成后生效）'
          : action === 'resume'
            ? '已请求继续'
            : '已请求重试失败页';
      pushToast(`${label}：${job.filename}`, updated.pause_requested ? 'warn' : 'neutral');
      jobs.reload();
      refreshCounts();
    } catch (caught) {
      setError(describeError(caught).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="page">
      <PageHeader
        icon={<IconJobs size={30} />}
        title="识别任务"
        description="本页显示的是本机服务保存的真实任务状态：暂停在当前页的安全提交点生效，重试失败页不会重做已成功的页。"
      />
      <div className="page-body">
        <div className="db-toolbar">
          <span className="db-name">任务</span>
          <span className="db-count num">{jobs.data ? `${num(jobs.data.total)} 个` : '读取中'}</span>
          <span className="db-count">{jobs.activeCount ? `${jobs.activeCount} 个进行中` : '当前没有进行中的任务'}</span>
          <div className="db-right">
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => {
                jobs.reload();
                refreshCounts();
              }}
            >
              立即刷新
            </button>
          </div>
        </div>

        {error ? (
          <div className="conflict-box" role="alert">
            {error}
          </div>
        ) : null}
        {jobs.error ? (
          <div className="conflict-box" role="alert">
            {jobs.error.message}
            <div className="btn-row" style={{ marginTop: 8 }}>
              <button type="button" className="btn btn-sm" onClick={jobs.reload}>
                重试
              </button>
            </div>
          </div>
        ) : null}

        {!items.length && !jobs.loading ? (
          <div className="empty">
            <strong>还没有识别任务</strong>
            在文献库批量导入 PDF 或 DOCX 后，本机服务会在这里创建后台任务。
          </div>
        ) : null}

        <div className="db-list">
          {items.map((job) => (
            <JobRow
              key={job.id}
              job={job}
              expanded={expanded === job.id}
              onToggle={() => setExpanded((current) => (current === job.id ? null : job.id))}
              onAction={(action) => void run(job, action)}
              busyKey={busy}
              onOpenUnit={openReader}
            />
          ))}
        </div>

        {pages > 1 ? (
          <div className="res-pager" style={{ marginTop: 12 }}>
            <button type="button" className="btn btn-sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>
              上一页
            </button>
            <span className="num">
              {num(page)} / {num(pages)}
            </span>
            <button type="button" className="btn btn-sm" disabled={page >= pages} onClick={() => setPage(page + 1)}>
              下一页
            </button>
          </div>
        ) : null}

        <p className="fine-print">
          <b>总页数未知</b>时不显示百分比，只显示已完成 / 待处理 / 失败页的实际计数。
          <br />
          关闭浏览器不会中止本机任务；服务重启后会从持久化状态继续。识别为空的页记为待处理，失败页需要重试或人工确认。
        </p>
      </div>
    </div>
  );
}

interface JobRowProps {
  job: JobItem;
  expanded: boolean;
  onToggle: () => void;
  onAction: (action: JobAction) => void;
  busyKey: string | null;
  onOpenUnit: (unitId: string) => void;
}

function JobRow({ job, expanded, onToggle, onAction, busyKey, onOpenUnit }: JobRowProps) {
  const state = jobStateInfo(job.state);
  const total = job.total_pages;
  // 候选任务：completed_pages 只表示识别推理跑完，绝不等于已人工采用。
  const candidateTask = job.kind === 'ocr_candidate';
  const donePct = total ? Math.round((job.completed_pages / total) * 100) : null;
  const pendPct = total ? Math.round((job.pending_pages / total) * 100) : null;
  const failPct = total ? Math.round((job.failed_pages / total) * 100) : null;
  const canPause = isActiveJob(job.state) && !job.pause_requested;
  const canResume = job.state === 'paused' || job.state === 'pausing';

  return (
    <div className="db-row">
      <div className="row-main">
        <div className="row-title">{job.filename}</div>
        <div className="row-meta">
          <Tag tone={state.tone} state>
            {job.pause_requested && isActiveJob(job.state) ? '正在暂停' : state.label}
          </Tag>
          {job.stage ? <Tag tone="neutral">{job.stage}</Tag> : null}
          <Tag tone="neutral">{methodLabel(job.kind)}</Tag>
          {job.duplicate ? <Tag tone="warn">重复文件，关联已有文献</Tag> : null}
          {job.review_pending_pages ? <Tag tone="info">待核对 {num(job.review_pending_pages)} 页</Tag> : null}
          {job.doc_id ? <span>已关联文献记录</span> : <span>尚未建立文献记录</span>}
        </div>

        {total ? (
          <div className="bar" role="img" aria-label={`已完成 ${job.completed_pages} 页，共 ${total} 页`}>
            <i style={{ width: `${donePct ?? 0}%` }} />
            <i className="b-pend" style={{ width: `${pendPct ?? 0}%` }} />
            <i className="b-fail" style={{ width: `${failPct ?? 0}%` }} />
          </div>
        ) : (
          <p className="help" style={{ margin: '8px 0 4px' }}>
            总页数统计中，暂不显示进度条。
          </p>
        )}

        <div className="job-line">
          <span className="num">共 {total === null ? '未知' : num(total)} 页</span>
          <span>
            {candidateTask ? '推理完成' : '已完成'} <span className="num">{num(job.completed_pages)}</span>
          </span>
          <span>
            待处理 <span className="num">{num(job.pending_pages)}</span>
          </span>
          <span>
            失败 <span className="num">{num(job.failed_pages)}</span>
          </span>
          <span>
            已处理 <span className="num">{num(job.processed_pages)}</span>
          </span>
          <span>
            可检索 <span className="num">{num(job.searchable_pages)}</span>
          </span>
          {typeof job.review_pending_pages === 'number' ? (
            <span>
              待核对 <span className="num">{num(job.review_pending_pages)}</span>
            </span>
          ) : null}
          <span>更新于 {formatTime(job.updated_at)}</span>
        </div>

        {candidateTask ? (
          <p className="help" style={{ marginTop: 4 }}>
            「推理完成」只表示识别推理跑完，不代表已人工采用；待核对页数单独统计，请在阅读器逐页决定。
          </p>
        ) : null}

        {job.error ? (
          <p className="error-text" style={{ marginTop: 6 }}>
            {job.error}
          </p>
        ) : null}

        <div className="btn-row" style={{ marginTop: 8 }}>
          <button
            type="button"
            className="btn btn-sm"
            disabled={!canPause || Boolean(busyKey)}
            onClick={() => onAction('pause')}
            title={job.pause_requested ? '已请求暂停，等待当前页提交' : '在当前页的安全提交点暂停'}
          >
            {job.pause_requested ? '正在暂停…' : '暂停'}
          </button>
          <button
            type="button"
            className="btn btn-sm"
            disabled={!canResume || Boolean(busyKey)}
            onClick={() => onAction('resume')}
          >
            继续
          </button>
          <button
            type="button"
            className="btn btn-sm"
            disabled={!job.failed_pages || Boolean(busyKey)}
            onClick={() => onAction('retry')}
            title="只重试失败页，不重做已成功的页"
          >
            重试失败页{job.failed_pages ? `（${job.failed_pages}）` : ''}
          </button>
          {job.doc_id ? (
            <button type="button" className="btn btn-sm btn-quiet" onClick={onToggle} aria-expanded={expanded}>
              {expanded ? '收起逐页状态' : '查看逐页状态'}
            </button>
          ) : null}
        </div>

        {expanded && job.doc_id ? (
          <JobUnitList jobId={job.id} docId={job.doc_id} jobKind={job.kind} onOpenUnit={onOpenUnit} />
        ) : null}
      </div>
    </div>
  );
}

/**
 * 逐页检查点：状态、尝试次数与失败原因都来自 GET /jobs/{id}/units。
 * position 是 0 起的任务内序号；范围任务只用服务端真实 file_page 标注文件页。
 * 未处理（work）的页可能还没有生成单元行，因此不允许打开阅读区。
 */
function JobUnitList({ jobId, docId, jobKind, onOpenUnit }: { jobId: string; docId: string; jobKind: string; onOpenUnit: (unitId: string) => void }) {
  const [page, setPage] = useState(1);
  const units = useApiQuery<Page<JobUnitItem>>(`job-units:${jobId}:${page}`, (signal) => listJobUnits(jobId, page, 30, signal), {
    poll: true,
  });
  // 文献类型决定位置标签的措辞：Word 的段落/表格单元不是书的物理页。
  const doc = useApiQuery<DocumentItem>(`job-unit-doc:${docId}`, (signal) => getDocument(docId, signal));
  const items = units.data?.items ?? [];
  const pages = units.data ? Math.max(1, Math.ceil(units.data.total / Math.max(1, units.data.page_size))) : 1;

  return (
    <div style={{ marginTop: 10 }}>
      <p className="help" style={{ marginTop: 0 }}>
        逐页检查点共 {num(units.data?.total ?? 0)} 项，来自本机服务持久化的任务记录。
      </p>
      {units.error ? <p className="error-text">{units.error.message}</p> : null}
      <ul className="file-list" style={{ maxHeight: 240 }}>
        {items.map((item) => {
          const state = jobUnitStateInfo(item.state);
          const openable = jobUnitOpenable(item.state);
          const location = typeof item.file_page === 'number' && item.file_page > 0
            ? `文件第 ${num(item.file_page)} 页`
            : jobKind === 'import' ? jobUnitLabel(doc.data?.filetype, item.position) : `任务第 ${num(item.position + 1)} 项`;
          return (
            <li key={item.unit_id}>
              {openable ? (
                <button
                  type="button"
                  className="f-name"
                  style={{ textAlign: 'left' }}
                  onClick={() => onOpenUnit(item.unit_id)}
                  title="在阅读区查看这一页"
                >
                  {location}
                </button>
              ) : (
                <span className="f-name" title="这一项还没有生成单元行，处理完成后才能打开">
                  {location}
                </span>
              )}
              <Tag tone={state.tone} state>
                {state.label}
              </Tag>
              <span className="num" style={{ color: 'var(--text-3)' }}>
                尝试 {num(item.attempts)} 次
              </span>
              {item.candidate_state ? (
                <Tag tone={item.candidate_state === 'ready' ? 'info' : 'neutral'}>
                  {candidateStateLabel(item.candidate_state)}
                </Tag>
              ) : null}
              {item.candidate_state === 'ready' && item.candidate_id ? (
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => onOpenUnit(item.unit_id)}
                  title="在阅读器「对照」里核对这一页的候选"
                >
                  进入对照
                </button>
              ) : null}
              {item.error ? <span className="error-text">{item.error}</span> : null}
            </li>
          );
        })}
      </ul>
      {!items.length && !units.loading ? <p className="help">本机服务还没有登记逐页检查点。</p> : null}
      {pages > 1 ? (
        <div className="res-pager" style={{ marginTop: 8 }}>
          <button type="button" className="btn btn-sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>
            上一页
          </button>
          <span className="num">
            {num(page)} / {num(pages)}
          </span>
          <button type="button" className="btn btn-sm" disabled={page >= pages} onClick={() => setPage(page + 1)}>
            下一页
          </button>
        </div>
      ) : null}
    </div>
  );
}
