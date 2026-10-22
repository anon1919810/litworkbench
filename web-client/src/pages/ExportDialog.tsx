/**
 * 导出对话框：先预览，确认条数与核验状态，再生成并下载。
 *
 * 三条硬约束（判断逻辑都在 ``lib/exportScope`` 里，这里只负责取数与呈现）：
 *
 * * 默认只导出**已接受**的摘录；要包含其它状态必须显式选「全部核验状态」；
 * * 范围/格式/核验口径一变，预览令牌立即作废，「生成」按钮回到禁用；
 * * 409 时保留设置、清空令牌并立刻重新预览，绝不输出上一次预览时的判断。
 *
 * 下载用服务端返回的固定地址与文件名（同源 + 会话 cookie），不经过 JS 传路径。
 * 本组件由父级在打开时挂载（关闭即卸载），所以每次打开都是全新的状态。
 */
import { useEffect, useReducer, useRef } from 'react';
import { createExport, exportDownloadUrl, previewExport } from '../api/endpoints';
import { Modal } from '../components/Modal';
import { describeError } from '../lib/conflict';
import { num } from '../lib/format';
import {
  buildExportRequest,
  canGenerate,
  currentKey,
  describeExportScope,
  describeReviewCounts,
  EXPORT_FORMATS,
  exportReducer,
  generateDisabledReason,
  initialExportState,
} from '../lib/exportScope';
import type { ExportDialogState, ExportFormat, ExportReviewScope } from '../lib/exportScope';
import type { EvidenceScope } from '../lib/evidenceSelection';

export function ExportDialog({
  scope,
  selectedIds,
  listTotal,
  listStale,
  onClose,
  onNotice,
}: {
  scope: EvidenceScope;
  selectedIds: string[];
  listTotal: number | null;
  listStale: boolean;
  onClose: () => void;
  onNotice: (text: string) => void;
}) {
  const [state, dispatch] = useReducer(exportReducer, initialExportState(
    scope, selectedIds, listTotal, listStale,
  ));
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => () => controllerRef.current?.abort(), []);

  const runPreview = async (target: ExportDialogState) => {
    const request = buildExportRequest(target);
    if (!request) return;
    const key = currentKey(target);
    controllerRef.current?.abort();
    const next = new AbortController();
    controllerRef.current = next;
    dispatch({ type: 'preview-request', key });
    try {
      const preview = await previewExport(request, next.signal);
      dispatch({ type: 'preview-ok', key, preview });
    } catch (caught) {
      const info = describeError(caught);
      if (info.kind === 'aborted') return;
      dispatch({ type: 'preview-fail', key, message: info.message });
    }
  };

  const generate = async () => {
    const request = buildExportRequest(state);
    const preview = state.preview;
    if (!request || !preview || !canGenerate(state)) return;
    dispatch({ type: 'generate-request' });
    try {
      const record = await createExport({ ...request, snapshot_token: preview.snapshot_token });
      dispatch({ type: 'generate-ok', record });
      triggerDownload(exportDownloadUrl(record.id), record.filename);
      onNotice(`资料集已生成：${record.filename}（${num(record.count)} 条）。`);
    } catch (caught) {
      const info = describeError(caught);
      if (info.kind === 'aborted') return;
      if (info.status === 409) {
        // 范围与设置原样保留；令牌作废，立刻重取预览。
        dispatch({ type: 'generate-conflict' });
        await runPreview(state);
        return;
      }
      dispatch({ type: 'generate-fail', message: info.message });
    }
  };

  const preview = state.preview;
  const disabledReason = generateDisabledReason(state);

  return (
    <Modal
      open
      title="导出摘录资料集"
      note="导出已保存的摘录，逐条保留出处、页码与核验状态。"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            关闭
          </button>
          <button
            type="button"
            className="btn"
            disabled={!buildExportRequest(state) || state.previewPending}
            onClick={() => void runPreview(state)}
          >
            {state.previewPending ? '预览中…' : preview ? '重新预览' : '预览'}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!canGenerate(state)}
            onClick={() => void generate()}
          >
            {state.generating ? '生成中…' : '生成并下载'}
          </button>
        </>
      }
    >
      <fieldset className="radio-row" style={{ marginBottom: 12 }}>
        <legend className="lbl">导出范围</legend>
        <label>
          <input
            type="radio"
            name="export-scope"
            checked={state.mode === 'filtered'}
            onChange={() => dispatch({ type: 'set-mode', mode: 'filtered' })}
          />
          <span>按下方条件导出全部匹配摘录（包含所有页）</span>
        </label>
        <label>
          <input
            type="radio"
            name="export-scope"
            checked={state.mode === 'selected'}
            disabled={state.selectedIds.length === 0}
            onChange={() => dispatch({ type: 'set-mode', mode: 'selected' })}
          />
          <span>仅导出已选的 {num(state.selectedIds.length)} 条中符合条件的摘录</span>
        </label>
      </fieldset>
      <p className="help">来源、分类和集合沿用列表，核验状态以下方选择为准。</p>

      <div className="field">
        <label className="lbl" htmlFor="export-format">
          格式
        </label>
        <select
          id="export-format"
          value={state.format}
          onChange={(event) => dispatch({ type: 'set-format', format: event.target.value as ExportFormat })}
        >
          {EXPORT_FORMATS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label className="lbl" htmlFor="export-review">
          核验状态
        </label>
        <select
          id="export-review"
          value={state.review}
          onChange={(event) => dispatch({ type: 'set-review', review: event.target.value as ExportReviewScope })}
        >
          <option value="accepted">仅已接受（默认）</option>
          <option value="all">全部核验状态（逐条标注，未核验条目不得直接引用）</option>
        </select>
      </div>

      <p className="help">{describeExportScope(state)}</p>

      {state.notice ? (
        <div className="notice-box" role="status">
          {state.notice}
        </div>
      ) : null}
      {state.previewError ? (
        <div className="conflict-box" role="alert">
          {state.previewError}
        </div>
      ) : null}
      {state.downloadError ? (
        <div className="conflict-box" role="alert">
          {state.downloadError}
        </div>
      ) : null}

      {preview ? (
        <div className="set-block" style={{ marginTop: 10 }}>
          <p className="set-h">
            预览：{num(preview.count)} 条 · {describeReviewCounts(preview.review_counts)}
          </p>
          <p className="set-sub">{preview.scope_description}</p>
          <p className="set-sub">
            文件名：<code>{preview.filename}</code>
          </p>
          <p className="set-sub">{preview.review_note}</p>
        </div>
      ) : null}

      {disabledReason ? <p className="help">{disabledReason}</p> : null}

      {state.download ? (
        <p className="set-sub" style={{ marginTop: 10 }}>
          已生成 {num(state.download.count)} 条。若下载未开始，可再次点「生成并下载」。
        </p>
      ) : null}

      <p className="help" style={{ marginTop: 12 }}>
        导出保留摘录时的引文；未核验、需复核和已拒绝的条目会附带提示。
      </p>
    </Modal>
  );
}

/** 一次性的 <a download> 触发同源下载：带会话 cookie，不经过 JS 传路径。 */
function triggerDownload(url: string, filename: string): void {
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = 'noopener';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}
