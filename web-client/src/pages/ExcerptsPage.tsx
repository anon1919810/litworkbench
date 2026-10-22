import { useEffect, useMemo, useState } from 'react';
import type { EvidenceItem, ReviewStatus } from '../api/types';
import { batchEvidence, patchEvidence } from '../api/endpoints';
import { IconExcerpts } from '../components/Icons';
import { Modal } from '../components/Modal';
import { PageHeader } from '../components/PageHeader';
import { Tag } from '../components/Tag';
import { useCollections, useDocuments, useEvidenceList } from '../hooks/useLibraryData';
import { describeError } from '../lib/conflict';
import {
  bulkAvailability,
  clearSelection,
  countSelected,
  createSelection,
  describeScope,
  emptyScope,
  reconcileSelection,
  scopeKey,
  selectedOnPage,
  setPageSelection,
  toggleOne,
} from '../lib/evidenceSelection';
import type { EvidenceScope, SelectionState } from '../lib/evidenceSelection';
import { formatTime, num, reviewStatusInfo, versionLabel } from '../lib/format';
import { useAppState } from '../state/AppState';
import { CollectionsDialog } from './CollectionsDialog';
import { ExportDialog } from './ExportDialog';

const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'all', label: '全部状态' },
  { value: 'pending', label: '待核验' },
  { value: 'accepted', label: '已接受' },
  { value: 'rejected', label: '已拒绝' },
  { value: 'stale', label: '需复核' },
];

export function ExcerptsPage() {
  const { openReader, pushToast, bumpDataVersion } = useAppState();
  const [page, setPage] = useState(1);
  const [scope, setScope] = useState<EvidenceScope>(emptyScope);
  const [selection, setSelection] = useState<SelectionState>(() => createSelection(scopeKey(emptyScope())));
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<EvidenceItem | null>(null);
  const [showCollections, setShowCollections] = useState(false);
  const [showExport, setShowExport] = useState(false);
  const [classifying, setClassifying] = useState(false);
  const [joining, setJoining] = useState('');

  const evidence = useEvidenceList(page, {
    docId: scope.docId,
    reviewStatus: scope.reviewStatus === 'all' ? '' : scope.reviewStatus,
    category: scope.category,
    collectionId: scope.collectionId,
  });
  const documents = useDocuments(1);
  const collections = useCollections();

  const items = evidence.data?.items ?? [];
  const collectionList = useMemo(() => collections.data ?? [], [collections.data]);
  // 列表正在按新条件加载时，旧行不对应当前条件：勾选与批量操作必须停用。
  const availability = bulkAvailability(selection, evidence.stale, evidence.loading);

  const changeScope = (patch: Partial<EvidenceScope>) => {
    const next = { ...scope, ...patch };
    setScope(next);
    setPage(1);
    const reconciled = reconcileSelection(selection, scopeKey(next));
    setSelection(reconciled.next);
    setNotice(reconciled.notice);
  };

  const selectedIds = selection.ids;
  const pageIds = items.map((item) => item.id);
  const allOnPage = pageIds.length > 0 && selectedOnPage(selection, pageIds) === pageIds.length;
  const activeCollection = collectionList.find((item) => item.id === scope.collectionId) ?? null;

  const runBatch = async (body: Parameters<typeof batchEvidence>[0], done: () => void) => {
    // 列表正在按新条件加载时，这些 ID 不一定还属于当前范围：直接拒绝执行。
    if (!availability.enabled) {
      pushToast(availability.reason ?? '当前不能批量操作。', 'warn');
      return;
    }
    try {
      const result = await batchEvidence(body);
      done();
      bumpDataVersion();
      pushToast(
        `已处理 ${num(result.evidence_ids.length)} 条：分类 ${num(result.updated)} 条、加入 ${num(result.added)} 条、移出 ${num(result.removed)} 条。`,
        'info',
      );
    } catch (caught) {
      pushToast(describeError(caught).message, 'error');
    }
  };

  return (
    <div className="page">
      <PageHeader
        icon={<IconExcerpts size={30} />}
        title="摘录集"
        description="按专题整理、分类和核验摘录，每条都保留文件页与印刷页码，并可返回来源。"
      />
      <div className="page-body">
        <div className="db-toolbar">
          <span className="db-name">摘录</span>
          <span className="db-count num">{evidence.data ? `${num(evidence.data.total)} 条` : '读取中'}</span>
          <div className="db-right">
            <label className="sr" htmlFor="excerpt-status">
              按核验状态筛选
            </label>
            <select
              id="excerpt-status"
              className="qselect"
              value={scope.reviewStatus}
              onChange={(event) => changeScope({ reviewStatus: event.target.value })}
            >
              {STATUS_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  状态：{option.label}
                </option>
              ))}
            </select>
            <label className="sr" htmlFor="excerpt-doc">
              按来源筛选
            </label>
            <select
              id="excerpt-doc"
              className="qselect"
              value={scope.docId}
              onChange={(event) => changeScope({ docId: event.target.value })}
            >
              <option value="">来源：全部文献</option>
              {(documents.data?.items ?? []).map((doc) => (
                <option key={doc.id} value={doc.id}>
                  {doc.title || doc.filename}
                </option>
              ))}
            </select>
            <label className="sr" htmlFor="excerpt-category">
              按分类筛选
            </label>
            <input
              id="excerpt-category"
              className="qselect"
              type="search"
              placeholder="分类（精确匹配）"
              list="excerpt-categories"
              value={scope.category}
              onChange={(event) => changeScope({ category: event.target.value })}
            />
            <datalist id="excerpt-categories">
              {Array.from(new Set(items.map((item) => item.category).filter(Boolean))).map((value) => (
                <option key={String(value)} value={String(value)} />
              ))}
            </datalist>
            <label className="sr" htmlFor="excerpt-collection">
              按集合筛选
            </label>
            <select
              id="excerpt-collection"
              className="qselect"
              value={scope.collectionId}
              onChange={(event) => changeScope({ collectionId: event.target.value })}
            >
              <option value="">集合：全部</option>
              {collectionList.map((collection) => (
                <option key={collection.id} value={collection.id}>
                  集合：{collection.name}（{num(collection.evidence_count)}）
                </option>
              ))}
            </select>
            <button type="button" className="btn btn-sm" onClick={() => setShowCollections(true)}>
              管理集合
            </button>
            <button type="button" className="btn btn-sm" onClick={evidence.reload}>
              刷新
            </button>
          </div>
        </div>

        {notice ? (
          <div className="notice-box" role="status">
            {notice}
          </div>
        ) : null}

        {evidence.error ? (
          <div className="conflict-box" role="alert">
            {evidence.error.message}
            <div className="btn-row" style={{ marginTop: 8 }}>
              <button type="button" className="btn btn-sm" onClick={evidence.reload}>
                重试
              </button>
            </div>
          </div>
        ) : null}

        <div className="sel-bar">
          <label className="sel-all">
            <input
              type="checkbox"
              checked={allOnPage}
              disabled={evidence.stale || items.length === 0}
              onChange={(event) =>
                setSelection((current) => setPageSelection(current, pageIds, event.target.checked))
              }
            />
            <span>本页全选（{num(selectedOnPage(selection, pageIds))} / {num(items.length)}）</span>
          </label>
          <span className="num">已选 {num(countSelected(selection))} 条（翻页保留）</span>
          <div className="btn-row">
            <button
              type="button"
              className="btn btn-sm"
              disabled={!availability.enabled}
              title={availability.reason ?? undefined}
              onClick={() => setClassifying(true)}
            >
              批量分类
            </button>
            <select
              className="qselect"
              aria-label="要加入的集合"
              value={joining}
              disabled={!availability.enabled || collectionList.length === 0}
              onChange={(event) => setJoining(event.target.value)}
            >
              <option value="">加入集合…</option>
              {collectionList.map((collection) => (
                <option key={collection.id} value={collection.id}>
                  {collection.name}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn btn-sm"
              disabled={!availability.enabled || !joining}
              onClick={() => {
                const collectionId = joining;
                void runBatch(
                  { evidence_ids: selectedIds, add_to_collection_id: collectionId },
                  () => setJoining(''),
                );
              }}
            >
              加入
            </button>
            <button
              type="button"
              className="btn btn-sm"
              disabled={!availability.enabled || !activeCollection}
              title={activeCollection ? undefined : '先用上方集合筛选选中一个集合，才能从中移出。'}
              onClick={() => {
                if (!activeCollection) return;
                void runBatch(
                  { evidence_ids: selectedIds, remove_from_collection_id: activeCollection.id },
                  () => setSelection(clearSelection()),
                );
              }}
            >
              移出当前集合
            </button>
            <button
              type="button"
              className="btn btn-sm btn-quiet"
              disabled={countSelected(selection) === 0}
              onClick={() => setSelection(clearSelection())}
            >
              清除选择
            </button>
            <button
              type="button"
              className="btn btn-sm btn-primary"
              onClick={() => setShowExport(true)}
            >
              导出资料集
            </button>
          </div>
          {availability.reason && countSelected(selection) > 0 ? (
            <span className="fine-print">{availability.reason}</span>
          ) : null}
        </div>

        {!items.length && !evidence.loading ? (
          <div className="empty">
            <strong>没有符合条件的摘录</strong>
            {scopeKey(scope) === scopeKey(emptyScope())
              ? '在阅读区选中文字后点「保存摘录」，摘录会出现在这里。'
              : `当前筛选：${describeScope(scope)}。换一个条件再试。`}
          </div>
        ) : null}

        <div className="db-list">
          {items.map((item) => (
            <ExcerptRow
              key={item.id}
              item={item}
              selected={selectedIds.includes(item.id)}
              selectable={!evidence.stale}
              onToggle={() => setSelection((current) => toggleOne(current, item.id))}
              onEdit={() => setEditing(item)}
              onJump={() => openReader(item.unit_id)}
              onChanged={() => {
                evidence.reload();
                bumpDataVersion();
                pushToast('摘录已更新。', 'info');
              }}
            />
          ))}
        </div>

        {evidence.totalPages > 1 ? (
          <div className="res-pager" style={{ marginTop: 12 }}>
            <button type="button" className="btn btn-sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>
              上一页
            </button>
            <span className="num">
              {num(page)} / {num(evidence.totalPages)}
            </span>
            <button type="button" className="btn btn-sm" disabled={page >= evidence.totalPages} onClick={() => setPage(page + 1)}>
              下一页
            </button>
          </div>
        ) : null}

        <p className="fine-print">
          <b>待核验</b>表示尚未人工确认；<b>已接受</b>表示已对照原页；<b>需复核</b>表示原文在校订或重新识别后发生了变化，引用仍指向原版本。
          颜色只作辅助，状态以文字标签为准。
        </p>
      </div>

      <ClassifyDialog
        open={classifying}
        count={selectedIds.length}
        onClose={() => setClassifying(false)}
        onSubmit={(category) => {
          setClassifying(false);
          void runBatch({ evidence_ids: selectedIds, category }, () => setSelection(clearSelection()));
        }}
      />

      <CollectionsDialog
        open={showCollections}
        collections={collectionList}
        onClose={() => setShowCollections(false)}
        onChanged={() => {
          collections.reload();
          bumpDataVersion();
        }}
      />

      {showExport ? (
        <ExportDialog
          scope={scope}
          selectedIds={selectedIds}
          listTotal={evidence.data?.total ?? null}
          listStale={evidence.stale}
          onClose={() => setShowExport(false)}
          onNotice={(text) => pushToast(text, 'info')}
        />
      ) : null}

      <ExcerptEditDialog
        item={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          evidence.reload();
          bumpDataVersion();
          pushToast('摘录已更新。', 'info');
        }}
      />
    </div>
  );
}

function ClassifyDialog({
  open,
  count,
  onClose,
  onSubmit,
}: {
  open: boolean;
  count: number;
  onClose: () => void;
  onSubmit: (category: string) => void;
}) {
  const [category, setCategory] = useState('');
  const [clear, setClear] = useState(false);

  useEffect(() => {
    if (open) {
      setCategory('');
      setClear(false);
    }
  }, [open]);

  if (!open) return null;
  return (
    <Modal
      open
      title="批量分类"
      note={`将把已选的 ${num(count)} 条摘录设为同一个分类。留空并勾选「清除分类」可以取消已有分类；核验状态需要逐条人工确认，不提供批量设置。`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!clear && !category.trim()}
            onClick={() => onSubmit(clear ? '' : category.trim())}
          >
            应用到 {num(count)} 条
          </button>
        </>
      }
    >
      <div className="field">
        <label className="lbl" htmlFor="batch-category">
          分类
        </label>
        <input
          id="batch-category"
          type="text"
          value={clear ? '' : category}
          disabled={clear}
          maxLength={100}
          onChange={(event) => setCategory(event.target.value)}
          style={{ width: '100%' }}
          placeholder="例如：文物建筑"
        />
      </div>
      <label className="radio-row">
        <input type="checkbox" checked={clear} onChange={(event) => setClear(event.target.checked)} />
        <span>清除这 {num(count)} 条摘录的分类</span>
      </label>
    </Modal>
  );
}

function ExcerptRow({
  item,
  selected,
  selectable,
  onToggle,
  onEdit,
  onJump,
  onChanged,
}: {
  item: EvidenceItem;
  selected: boolean;
  selectable: boolean;
  onToggle: () => void;
  onEdit: () => void;
  onJump: () => void;
  onChanged: () => void;
}) {
  const review = reviewStatusInfo(item.review_status);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const setReview = async (next: ReviewStatus) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await patchEvidence(item.id, { review_status: next });
      onChanged();
    } catch (caught) {
      setError(describeError(caught).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="db-row">
      <label className="sel-cell">
        <input
          type="checkbox"
          checked={selected}
          disabled={!selectable}
          onChange={onToggle}
          aria-label={`选择摘录：${item.quote.slice(0, 20)}`}
        />
      </label>
      <div className="row-main">
        <div className="row-title" style={{ fontWeight: 400, fontSize: 14.5 }}>
          {item.quote}
        </div>
        <div className="row-meta">
          <Tag tone={review.tone} state stale={item.review_status === 'stale'}>
            {review.label}
          </Tag>
          {item.category ? <Tag tone="neutral">{item.category}</Tag> : null}
          <span>{item.source.title || item.source.filename}</span>
          <span className="num">
            {item.source.file_page === null ? `文档单元 ${num(item.source.seq ?? 0)}` : `文件第 ${num(item.source.file_page)} 页`}
            {item.source.printed_page ? ` · 印刷页 ${item.source.printed_page}` : ''}
          </span>
          <span className="num">
            {versionLabel(item.version)} · 偏移 {num(item.start_offset)}–{num(item.end_offset)}
          </span>
          <span>{formatTime(item.created_at)}</span>
        </div>
        {item.note ? (
          <p className="row-sub">备注：{item.note}</p>
        ) : null}
        {item.review_note ? (
          <p className="row-sub">核验记录：{item.review_note}</p>
        ) : null}
        {error ? <p className="error-text">{error}</p> : null}
      </div>
      <div className="row-acts">
        <button type="button" className="btn btn-sm" disabled={busy} onClick={() => onEdit()}>
          编辑备注
        </button>
        <button type="button" className="btn btn-sm" onClick={onJump}>
          返回来源
        </button>
        <button
          type="button"
          className="btn btn-sm"
          disabled={busy || item.review_status === 'accepted'}
          onClick={() => void setReview('accepted')}
          title="标记为已对照原页核验"
        >
          标记已接受
        </button>
        <button
          type="button"
          className="btn btn-sm btn-quiet"
          disabled={busy || item.review_status === 'rejected'}
          onClick={() => void setReview('rejected')}
        >
          标记已拒绝
        </button>
      </div>
    </div>
  );
}

function ExcerptEditDialog({
  item,
  onClose,
  onSaved,
}: {
  item: EvidenceItem | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [note, setNote] = useState('');
  const [reviewNote, setReviewNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const itemId = item?.id ?? null;

  useEffect(() => {
    if (!item) return;
    setNote(item.note ?? '');
    setReviewNote(item.review_note ?? '');
    setError(null);
  }, [itemId]); // eslint-disable-line react-hooks/exhaustive-deps -- 打开某一条时载入其草稿

  if (!item) return null;

  return (
    <Modal
      open
      title="编辑摘录"
      note="修改备注不会改写引文原文。"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={saving}
            onClick={async () => {
              setSaving(true);
              setError(null);
              try {
                await patchEvidence(item.id, { note, review_note: reviewNote });
                onSaved();
                onClose();
              } catch (caught) {
                setError(describeError(caught).message);
              } finally {
                setSaving(false);
              }
            }}
          >
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
      <div className="quote-box" style={{ marginBottom: 14 }}>
        {item.quote}
      </div>
      <p className="help" style={{ marginTop: 0 }}>
        {item.source.locator ??
          `${item.source.title || item.source.filename}${item.source.file_page === null ? '' : ` · 文件第 ${item.source.file_page} 页`}${item.source.printed_page ? ` · 印刷页 ${item.source.printed_page}` : ''}`}
      </p>
      <div className="field">
        <label className="lbl" htmlFor="excerpt-edit-note">
          分析备注
        </label>
        <textarea id="excerpt-edit-note" value={note} onChange={(event) => setNote(event.target.value)} rows={4} />
      </div>
      <div className="field">
        <label className="lbl" htmlFor="excerpt-edit-review">
          核验记录
        </label>
        <input
          id="excerpt-edit-review"
          type="text"
          value={reviewNote}
          onChange={(event) => setReviewNote(event.target.value)}
          style={{ width: '100%' }}
          placeholder="例如：已对照扫描页确认，页码 775"
        />
      </div>
    </Modal>
  );
}
