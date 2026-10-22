import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode, RefObject } from 'react';
import { documentSourceUrl, getDocument, RENDER_SCALES } from '../api/endpoints';
import type { RenderScale } from '../api/endpoints';
import type { UnitDetail, UnitSummary } from '../api/types';
import { useApiQuery } from '../hooks/useApiQuery';
import { useDocumentUnits, useUnit } from '../hooks/useLibraryData';
import type { ErrorInfo } from '../lib/conflict';
import { draftFromSelection } from '../lib/excerpt';
import type { QuoteDraft } from '../lib/excerpt';
import { readSelectionSlice } from '../lib/selection';
import {
  filePageLabel,
  isCorrectedVersion,
  methodLabel,
  num,
  printedPageLabel,
  printedPageStatusLabel,
  unitStatusInfo,
  versionLabel,
} from '../lib/format';
import { SourceImage } from '../components/SourceImage';
import { Tag } from '../components/Tag';
import { UnitText } from '../components/UnitText';
import { useAppState } from '../state/AppState';
import { CorrectionEditor } from './CorrectionEditor';
import { ExcerptDialog } from './ExcerptDialog';
import { ConfirmBlankButton, PrintedPageDialog } from './PrintedPageDialog';
import { OcrCandidatesPanel } from './OcrCandidatesPanel';
import type { OcrCandidateView } from './OcrCandidatesPanel';
import { ReprocessDialog } from './ReprocessDialog';

const UNITS_PAGE_SIZE = 30;

type ReaderTab = 'scan' | 'text' | 'compare';

interface ReaderProps {
  unitId: string | null;
  onSelectUnit: (unitId: string) => void;
  onBack?: () => void;
  emptyHint?: ReactNode;
}

type PendingJump = { page: number; seq?: number; edge?: 'first' | 'last' };

/**
 * 阅读区：原件 / 识别文字 / 对照。
 * 只加载当前单元的文字与当前页的渲染图；翻页通过文档单元分页接口，不缓存整本书。
 */
export function Reader({ unitId, onSelectUnit, onBack, emptyHint }: ReaderProps) {
  const { pushToast, bumpDataVersion } = useAppState();
  const unit = useUnit(unitId);
  const [tab, setTab] = useState<ReaderTab>('scan');
  const [scale, setScale] = useState<RenderScale>(1.5);
  const [zoom, setZoom] = useState<'fit' | 'actual'>('fit');
  const [unitsPage, setUnitsPage] = useState(1);
  const [jumpValue, setJumpValue] = useState('');
  const [pendingJump, setPendingJump] = useState<PendingJump | null>(null);
  const [excerptOpen, setExcerptOpen] = useState(false);
  const [excerptDraft, setExcerptDraft] = useState<QuoteDraft | null>(null);
  const [reselectHint, setReselectHint] = useState<string | null>(null);
  const [printedOpen, setPrintedOpen] = useState(false);
  const [reprocessOpen, setReprocessOpen] = useState(false);
  /** 最近一次提交的候选任务：用于首次生成时轮询「还没有候选行」的页。 */
  const [candidateJobId, setCandidateJobId] = useState<string | null>(null);
  /** 对照面板当前显示的是现用/候选/旧 OCR；非「现用」时禁用全局「保存摘录」。 */
  const [candidateView, setCandidateView] = useState<OcrCandidateView>('current');
  const textRef = useRef<HTMLParagraphElement>(null);

  const detail = unit.data;
  const docId = detail?.doc_id ?? null;

  const doc = useApiQuery(
    detail ? `document:${docId}` : 'document:none',
    (signal) => getDocument(docId as string, signal),
    { enabled: Boolean(docId) },
  );
  const units = useDocumentUnits(docId, unitsPage);

  // 切换单元时收起所有对话框并丢弃旧草稿，避免把上一单元的编辑内容带到新页。
  useEffect(() => {
    setJumpValue('');
    setExcerptOpen(false);
    setExcerptDraft(null);
    setReselectHint(null);
    setPrintedOpen(false);
    setReprocessOpen(false);
    setCandidateJobId(null);
    setCandidateView('current');
  }, [unitId]);

  // 打开的单元不在当前分页时，自动跳到它所在的分页。
  useEffect(() => {
    if (!detail || !units.data) return;
    const total = units.data.total;
    if (!total) return;
    const targetPage = Math.min(Math.max(1, Math.ceil(detail.seq / UNITS_PAGE_SIZE)), Math.ceil(total / UNITS_PAGE_SIZE));
    setUnitsPage((current) => (current === targetPage ? current : targetPage));
  }, [detail, units.data]);

  // 翻页或跳转后，从新分页里取目标单元。
  useEffect(() => {
    if (!pendingJump || !units.data || units.data.page !== pendingJump.page) return;
    const items = units.data.items;
    const target =
      pendingJump.seq !== undefined
        ? items.find((item) => item.seq === pendingJump.seq)
        : pendingJump.edge === 'last'
          ? items[items.length - 1]
          : items[0];
    setPendingJump(null);
    if (target && target.id !== unitId) onSelectUnit(target.id);
  }, [pendingJump, units.data, unitId, onSelectUnit]);

  const currentIndex = useMemo(() => {
    const items = units.data?.items ?? [];
    return items.findIndex((item) => item.id === unitId);
  }, [units.data, unitId]);

  const totalUnits = units.data?.total ?? 0;
  const totalPages = totalUnits ? Math.ceil(totalUnits / UNITS_PAGE_SIZE) : 1;

  const gotoAdjacent = (delta: -1 | 1) => {
    const items = units.data?.items ?? [];
    const next = items[currentIndex + delta];
    if (next) {
      onSelectUnit(next.id);
      return;
    }
    const targetPage = unitsPage + delta;
    if (targetPage < 1 || targetPage > totalPages) return;
    setUnitsPage(targetPage);
    setPendingJump({ page: targetPage, edge: delta === 1 ? 'first' : 'last' });
  };

  const submitJump = (raw: string) => {
    const value = Number(raw.trim());
    if (!Number.isInteger(value) || value < 1 || value > totalUnits) return;
    const page = Math.min(Math.max(1, Math.ceil(value / UNITS_PAGE_SIZE)), totalPages);
    setUnitsPage(page);
    setPendingJump({ page, seq: value });
  };

  /**
   * 点“保存摘录”时立刻读取选区：对话框是模态的，打开后浏览器会清掉选区，
   * 所以偏移必须在这一刻算好，而不是在对话框里再读一次。
   */
  const openExcerpt = useCallback(() => {
    if (!detail) return;
    // 候选/旧 OCR 显示时不允许把面板里的文字当正式摘录保存。
    if (candidateView !== 'current') return;
    const slice = textRef.current ? readSelectionSlice(textRef.current, window) : null;
    const fromSelection = slice ? draftFromSelection(detail.text, slice) : null;
    setExcerptDraft(fromSelection ?? excerptDraft);
    setReselectHint(null);
    setExcerptOpen(true);
  }, [detail, excerptDraft, candidateView]);

  /** 关闭摘录对话框；keepDraft 非空表示用户要求保留草稿并重新选区。 */
  const closeExcerpt = (keepDraft?: QuoteDraft | null) => {
    setExcerptOpen(false);
    if (keepDraft) {
      setExcerptDraft(keepDraft);
      setReselectHint('草稿已保留：在“识别文字”里重新选中文字后，再点一次「保存摘录」。');
    }
  };

  if (!unitId) {
    return (
      <div className="reader">
        <div className="doc-head">
          <div>
            <h2 className="doc-title">未选择页面</h2>
            <p className="doc-sub">{emptyHint ?? '在左侧结果中选择一页，即可查看原页、识别文字与对照。'}</p>
          </div>
        </div>
      </div>
    );
  }

  // 切换到新 unitId 时 detail 仍是上一页的数据（stale）：先显示加载/错误，绝不把上一页详情当作新页展示。
  if (!unit.error && (unit.stale || (unit.loading && !detail))) {
    return (
      <div className="reader">
        <div className="doc-head">
          <div>
            <h2 className="doc-title">正在读取单元…</h2>
            <p className="doc-sub">只读取当前单元的文字与页面图。</p>
          </div>
        </div>
      </div>
    );
  }

  if (unit.error && (!detail || unit.stale)) {
    return (
      <div className="reader">
        <div className="doc-head">
          <div>
            <h2 className="doc-title">无法读取该单元</h2>
            <p className="doc-sub">{unit.error.message}</p>
            <div className="btn-row" style={{ marginTop: 10 }}>
              <button type="button" className="btn btn-sm" onClick={unit.reload}>
                重试
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (!detail) return <div className="reader" />;

  return (
    <ReaderBody
      detail={detail}
      documentTitle={doc.data?.title ?? detail.title}
      documentFilename={detail.filename}
      pageCount={doc.data?.page_count ?? null}
      tab={tab}
      setTab={setTab}
      scale={scale}
      setScale={(value) => setScale(RENDER_SCALES.includes(value) ? value : 1.5)}
      zoom={zoom}
      setZoom={setZoom}
      onBack={onBack}
      onSaveExcerpt={openExcerpt}
      excerptDisabled={candidateView !== 'current'}
      reselectHint={reselectHint}
      onEditPrintedPage={() => setPrintedOpen(true)}
      onReprocess={() => setReprocessOpen(true)}
      textRef={textRef}
      onSaved={() => {
        unit.reload();
        bumpDataVersion();
      }}
      candidateJobId={candidateJobId}
      onCandidateViewChange={setCandidateView}
      onSelectUnit={onSelectUnit}
      pager={
        <UnitPager
          currentIndex={currentIndex}
          items={units.data?.items ?? []}
          page={unitsPage}
          totalPages={totalPages}
          totalUnits={totalUnits}
          jumpValue={jumpValue}
          setJumpValue={setJumpValue}
          onPrev={() => gotoAdjacent(-1)}
          onNext={() => gotoAdjacent(1)}
          onJump={submitJump}
          loading={units.loading}
          error={units.error}
        />
      }
      excerptDialog={
        excerptOpen ? (
          <ExcerptDialog
            open
            unit={detail}
            pageCount={doc.data?.page_count ?? null}
            initialDraft={excerptDraft}
            onClose={closeExcerpt}
            onSaved={bumpDataVersion}
          />
        ) : null
      }
      printedDialog={printedOpen ? <PrintedPageDialog unit={detail} onClose={() => setPrintedOpen(false)} onSaved={unit.reload} /> : null}
      reprocessDialog={
        reprocessOpen ? (
          <ReprocessDialog
            unit={detail}
            onClose={() => setReprocessOpen(false)}
            onQueued={(message, job) => {
              pushToast(message, 'info');
              if (job) {
                setCandidateJobId(job.id);
                setTab('compare');
              }
            }}
          />
        ) : null
      }
    />
  );
}

interface ReaderBodyProps {
  detail: UnitDetail;
  documentTitle: string | null;
  documentFilename: string;
  pageCount: number | null;
  tab: ReaderTab;
  setTab: (tab: ReaderTab) => void;
  scale: RenderScale;
  setScale: (scale: RenderScale) => void;
  zoom: 'fit' | 'actual';
  setZoom: (zoom: 'fit' | 'actual') => void;
  onBack?: () => void;
  onSaveExcerpt: () => void;
  /** 候选/旧 OCR 显示时为 true：禁用全局「保存摘录」。 */
  excerptDisabled: boolean;
  reselectHint: string | null;
  onEditPrintedPage: () => void;
  onReprocess: () => void;
  textRef: RefObject<HTMLParagraphElement | null>;
  onSaved: () => void;
  candidateJobId: string | null;
  onCandidateViewChange: (view: OcrCandidateView) => void;
  onSelectUnit: (unitId: string) => void;
  pager: ReactNode;
  excerptDialog: ReactNode;
  printedDialog: ReactNode;
  reprocessDialog: ReactNode;
}

function ReaderBody(props: ReaderBodyProps) {
  const {
    detail,
    documentTitle,
    documentFilename,
    pageCount,
    tab,
    setTab,
    scale,
    setScale,
    zoom,
    setZoom,
    onBack,
    onSaveExcerpt,
    excerptDisabled,
    reselectHint,
    onEditPrintedPage,
    onReprocess,
    textRef,
    onSaved,
    candidateJobId,
    onCandidateViewChange,
    onSelectUnit,
    pager,
    excerptDialog,
    printedDialog,
    reprocessDialog,
  } = props;

  const isDocx = detail.kind.toLowerCase().includes('doc') || documentFilename.toLowerCase().endsWith('.docx');
  const status = unitStatusInfo(detail.status);
  const printedState = printedPageStatusLabel(detail.printed_page_status);
  const hasText = detail.text.trim().length > 0;
  const corrected = isCorrectedVersion(detail.version);

  const tabs: Array<{ id: ReaderTab; label: string; disabled?: boolean; hint?: string }> = [
    { id: 'scan', label: '原件', disabled: isDocx, hint: 'Word 文档没有页面渲染，请使用“识别文字”。' },
    { id: 'text', label: '识别文字' },
    { id: 'compare', label: '对照', disabled: isDocx, hint: 'Word 文档只有文字单元，没有可对照的页面图。' },
  ];
  const activeTab: ReaderTab = isDocx && tab !== 'text' ? 'text' : tab;

  const textBlock = (
    <>
      <div className="read-meta">
        <Tag tone={status.tone} state>
          {status.label}
        </Tag>
        <Tag tone="neutral">{methodLabel(detail.method)}</Tag>
        <Tag tone={corrected ? 'info' : 'neutral'}>{versionLabel(detail.version)}</Tag>
        {printedState ? (
          <Tag tone={detail.printed_page_status === 'confirmed' ? 'info' : 'warn'}>{printedState}页码</Tag>
        ) : null}
        {typeof detail.ocr?.mean_confidence === 'number' ? (
          <Tag tone={detail.ocr.mean_confidence < 0.8 ? 'warn' : 'neutral'}>
            平均置信度 {detail.ocr.mean_confidence.toFixed(2)}
          </Tag>
        ) : null}
      </div>
      {hasText ? (
        <>
          <UnitText ref={textRef} text={detail.text} />
          <p className="fine-print">
            在上面选中文字后点右上角「保存摘录」，保存的是这次选中的精确切片；也可以直接编辑引文并选择出现次序。
          </p>
        </>
      ) : (
        <div className="scan-ph">
          <b>本页没有可读文字</b>
          {detail.status === 'pending'
            ? '识别结果为空，已计为待处理。可确认为空白页，或对该页发起强制重新识别。'
            : `当前状态为「${detail.status}」。可在属性栏查看备注，或对该页发起强制重新识别。`}
        </div>
      )}
      {detail.notes ? (
        <div className="note-box" style={{ marginTop: 18, borderTop: '1px solid var(--line)' }}>
          <h4>本页备注</h4>
          <p style={{ fontSize: 13, lineHeight: 1.8 }}>{detail.notes}</p>
        </div>
      ) : null}
      {detail.revisions.length ? (
        <div style={{ marginTop: 14 }}>
          <h4 style={{ fontSize: 12.5, color: 'var(--text-2)', marginBottom: 6 }}>修改历史</h4>
          <ul className="note-list">
            {detail.revisions.slice(-5).map((revision, index) => (
              <li key={`${revision.at}-${index}`}>
                {revision.kind}
                {revision.reason ? ` · ${revision.reason}` : ''}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </>
  );

  return (
    <div className="reader">
      <div className="doc-head">
        {onBack ? (
          <button type="button" className="btn btn-sm only-sm" onClick={onBack}>
            ← 结果
          </button>
        ) : null}
        <div style={{ minWidth: 0, flex: '1 1 auto' }}>
          <h2 className="doc-title" title={documentTitle || documentFilename}>
            {documentTitle || documentFilename}
          </h2>
          <p className="doc-sub">
            {detail.title_path ? `${detail.title_path} · ` : ''}
            {filePageLabel(detail.file_page, pageCount)}
          </p>
        </div>
        <div className="doc-acts">
          <button
            type="button"
            className="btn btn-sm"
            onClick={onReprocess}
            title="只重新识别本页或整本；旧结果在提交前保留"
          >
            重新识别
          </button>
          <button
            type="button"
            className="btn btn-sm btn-primary"
            onClick={onSaveExcerpt}
            disabled={excerptDisabled}
            title={
              excerptDisabled
                ? '当前显示候选或旧 OCR 文字：请切回「现用文字」再保存摘录。'
                : '保存当前选中的精确切片'
            }
          >
            保存摘录
          </button>
        </div>
      </div>

      {reselectHint ? (
        <div className="notice-box" style={{ margin: '0 24px 10px' }} role="status">
          {reselectHint}
        </div>
      ) : null}

      <details className="source-properties">
        <summary>
          <span>{filePageLabel(detail.file_page, pageCount)}</span>
          <span aria-hidden="true">·</span>
          <span>{printedPageLabel(detail.printed_page, detail.printed_page_status)}</span>
          <Tag tone={status.tone} state>
            {status.label}
          </Tag>
        </summary>
        <div className="props">
          <div className="prop">
            <span className="pk">来源文件</span>
            <span className="pv" title={documentFilename}>
              {documentFilename}
            </span>
          </div>
          <div className="prop">
            <span className="pk">书名</span>
            <span className="pv">{documentTitle || '未填写'}</span>
          </div>
          <div className="prop">
            <span className="pk">单元序号</span>
            <span className="pv num">{detail.seq}</span>
          </div>
          <div className="prop">
            <span className="pk">标题路径</span>
            <span className="pv">{detail.title_path || '—'}</span>
          </div>
          <div className="prop">
            <span className="pk">处理方式</span>
            <span className="pv">{methodLabel(detail.method)}</span>
          </div>
          <div className="prop">
            <span className="pk">文本版本</span>
            <span className="pv">
              {versionLabel(detail.version)} · 哈希 <span title={detail.text_sha256}>{detail.text_sha256.slice(0, 12)}…</span>
            </span>
          </div>
          <div className="prop">
            <span className="pk">页码操作</span>
            <span className="pv">
              <button type="button" className="btn btn-sm" onClick={onEditPrintedPage}>
                编辑印刷页码
              </button>
              <ConfirmBlankButton unit={detail} onSaved={onSaved} />
            </span>
          </div>
        </div>
      </details>

      <div className="doc-tabs" role="tablist" aria-label="阅读方式">
        {tabs.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            id={`tab-${item.id}`}
            className="dtab"
            aria-selected={activeTab === item.id}
            aria-controls={`panel-${item.id}`}
            disabled={item.disabled}
            title={item.hint}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="panel" role="tabpanel" id={`panel-${activeTab}`} aria-labelledby={`tab-${activeTab}`}>
        {activeTab === 'scan' ? (
          <SourceImage unit={detail} scale={scale} onScaleChange={setScale} zoom={zoom} onZoomChange={setZoom} active />
        ) : null}
        {activeTab === 'text' ? <div className="read-wrap">{textBlock}</div> : null}
        {activeTab === 'compare' ? (
          <div className="cmp">
            <div className="cmp-col">
              <div className="cmp-head">
                <span>原件</span>
                <Tag tone="neutral">{documentFilename}</Tag>
              </div>
              <div className="cmp-scroll">
                <SourceImage unit={detail} scale={scale} onScaleChange={setScale} zoom={zoom} onZoomChange={setZoom} active />
              </div>
            </div>
            <div className="cmp-col">
              <div className="cmp-head">
                <span>识别文字</span>
                <Tag tone={status.tone} state>
                  {status.label}
                </Tag>
              </div>
              <div className="cmp-text">
                <OcrCandidatesPanel
                  key={detail.unit_id}
                  unit={detail}
                  onSaved={onSaved}
                  onSelectUnit={onSelectUnit}
                  jobId={candidateJobId ?? undefined}
                  onViewChange={onCandidateViewChange}
                  currentTextRef={textRef}
                />
                <CorrectionEditor unit={detail} onSaved={onSaved} />
              </div>
            </div>
          </div>
        ) : null}
      </div>

      <div className="srcbar">
        <div className="src-info">
          <span>
            {filePageLabel(detail.file_page, pageCount)} · {printedPageLabel(detail.printed_page, detail.printed_page_status)}
          </span>
          <Tag tone={status.tone} state>
            {status.label}
          </Tag>
          <Tag tone="neutral">{versionLabel(detail.version)}</Tag>
          <span>{hasText ? '可选择文字保存摘录' : '本页无文字'}</span>
        </div>
        <div className="src-acts">
          <a className="btn btn-sm" href={documentSourceUrl(detail.doc_id)} download>
            下载原件
          </a>
          {pager}
        </div>
      </div>

      {excerptDialog}
      {printedDialog}
      {reprocessDialog}
    </div>
  );
}

interface UnitPagerProps {
  currentIndex: number;
  items: UnitSummary[];
  page: number;
  totalPages: number;
  totalUnits: number;
  jumpValue: string;
  setJumpValue: (value: string) => void;
  onPrev: () => void;
  onNext: () => void;
  onJump: (value: string) => void;
  loading: boolean;
  error: ErrorInfo | null;
}

/** 文档单元翻页：上一页 / 下一页 / 序号跳转，全部来自服务端分页结果。 */
function UnitPager(props: UnitPagerProps) {
  const { currentIndex, items, page, totalPages, totalUnits, jumpValue, setJumpValue, onPrev, onNext, onJump, loading, error } =
    props;
  const hasPrev = currentIndex > 0 || page > 1;
  const hasNext = (currentIndex >= 0 && currentIndex < items.length - 1) || page < totalPages;
  const position = currentIndex >= 0 ? (page - 1) * UNITS_PAGE_SIZE + currentIndex + 1 : null;

  return (
    <div className="pager">
      <button type="button" className="btn btn-sm" onClick={onPrev} disabled={!hasPrev || loading}>
        ← 上一页
      </button>
      <span className="num pager-count" style={{ fontSize: 12.5, color: 'var(--text-3)' }}>
        {position === null ? '当前页未在列表中' : `第 ${num(position)} 个单元 / 共 ${num(totalUnits)}`}
      </span>
      <button type="button" className="btn btn-sm" onClick={onNext} disabled={!hasNext || loading}>
        下一页 →
      </button>
      <form
        className="pager"
        onSubmit={(event) => {
          event.preventDefault();
          onJump(jumpValue);
        }}
      >
        <label className="sr" htmlFor="unit-jump">
          跳转到第几个单元
        </label>
        <input
          id="unit-jump"
          type="number"
          min={1}
          max={Math.max(1, totalUnits)}
          placeholder={`1–${totalUnits}`}
          value={jumpValue}
          onChange={(event) => setJumpValue(event.target.value)}
          disabled={!totalUnits}
        />
        <button type="button" className="btn btn-sm" onClick={() => onJump(jumpValue)} disabled={!totalUnits || loading}>
          跳转
        </button>
      </form>
      {error ? <span className="error-text">{error.message}</span> : null}
    </div>
  );
}
