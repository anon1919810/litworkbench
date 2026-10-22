import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { uploadImport } from '../api/endpoints';
import type { ImportResult, OcrMode } from '../api/types';
import { describeError } from '../lib/conflict';
import { formatBytes } from '../lib/format';
import { Modal } from '../components/Modal';
import { Tag } from '../components/Tag';

const ACCEPT = '.pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document';
const MAX_BYTES = 1024 * 1024 * 1024; // 与契约一致：单文件 1GiB

const OCR_MODES: Array<{ value: OcrMode; label: string; hint: string }> = [
  { value: 'auto', label: '自动（推荐）', hint: '有可用文本层就直接使用，没有再用 OCR。' },
  { value: 'off', label: '只用文本层', hint: '不执行 OCR；没有文本层的页会保持待处理。' },
  { value: 'force', label: '强制 OCR', hint: '忽略文本层，逐页重新识别，适合坏文本层。' },
];

type Phase = 'pick' | 'uploading' | 'done';

interface ImportDialogProps {
  open: boolean;
  onClose: () => void;
  onQueued: () => void;
}

export function ImportDialog({ open, onClose, onQueued }: ImportDialogProps) {
  const [files, setFiles] = useState<File[]>([]);
  const [ocrMode, setOcrMode] = useState<OcrMode>('auto');
  const [phase, setPhase] = useState<Phase>('pick');
  const [progress, setProgress] = useState<{ loaded: number; total: number | null }>({ loaded: 0, total: null });
  const [result, setResult] = useState<ImportResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const dirRef = useRef<HTMLInputElement>(null);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!open) {
      controllerRef.current?.abort();
      setFiles([]);
      setPhase('pick');
      setProgress({ loaded: 0, total: null });
      setResult(null);
      setError(null);
      setDragging(false);
    }
  }, [open]);

  const addFiles = useCallback((incoming: FileList | null) => {
    if (!incoming) return;
    const next: File[] = [];
    const seen = new Set<string>();
    for (const file of Array.from(incoming)) {
      const key = `${file.name}:${file.size}:${file.lastModified}`;
      if (seen.has(key)) continue;
      seen.add(key);
      next.push(file);
    }
    setFiles((current) => {
      const merged = [...current];
      const keys = new Set(current.map((file) => `${file.name}:${file.size}:${file.lastModified}`));
      for (const file of next) {
        const key = `${file.name}:${file.size}:${file.lastModified}`;
        if (!keys.has(key)) {
          merged.push(file);
          keys.add(key);
        }
      }
      return merged;
    });
  }, []);

  const oversize = useMemo(() => files.filter((file) => file.size > MAX_BYTES), [files]);
  const totalBytes = useMemo(() => files.reduce((sum, file) => sum + file.size, 0), [files]);
  const percent = progress.total ? Math.min(100, Math.round((progress.loaded / progress.total) * 100)) : null;

  const start = async () => {
    if (!files.length) return;
    const controller = new AbortController();
    controllerRef.current = controller;
    setPhase('uploading');
    setError(null);
    setResult(null);
    setProgress({ loaded: 0, total: totalBytes });
    try {
      const response = await uploadImport({
        files,
        ocrMode,
        signal: controller.signal,
        onProgress: setProgress,
      }).promise;
      setResult(response);
      setPhase('done');
      onQueued();
    } catch (caught) {
      const info = describeError(caught);
      if (info.kind === 'aborted') {
        setPhase('pick');
        return;
      }
      setError(info.message);
      setPhase('pick');
    }
  };

  return (
    <Modal
      open={open}
      wide
      title="批量导入文献"
      note="文件直接上传到本机 127.0.0.1 的文献服务，不发送到任何第三方；上传完成后的文字识别在后台进行。"
      onClose={() => {
        controllerRef.current?.abort();
        onClose();
      }}
      footer={
        phase === 'uploading' ? (
          <button
            type="button"
            className="btn"
            onClick={() => {
              controllerRef.current?.abort();
            }}
          >
            取消上传
          </button>
        ) : (
          <>
            <button type="button" className="btn" onClick={onClose}>
              关闭
            </button>
            <button type="button" className="btn btn-primary" onClick={() => void start()} disabled={!files.length || oversize.length > 0}>
              上传到本机
            </button>
          </>
        )
      }
    >
      {error ? (
        <div className="conflict-box" role="alert">
          {error}
        </div>
      ) : null}

      <div className={dragging ? 'file-pick active' : 'file-pick'} onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }} onDragLeave={() => setDragging(false)} onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        addFiles(event.dataTransfer.files);
      }}>
        <p style={{ margin: 0 }}>
          把 PDF / DOCX 文件拖到这里，或
          <button
            type="button"
            className="btn btn-sm"
            style={{ margin: '0 4px' }}
            onClick={() => inputRef.current?.click()}
            disabled={phase === 'uploading'}
          >
            选择文件
          </button>
          选择多个文件；
          <button
            type="button"
            className="btn btn-sm"
            style={{ margin: '0 4px' }}
            onClick={() => dirRef.current?.click()}
            disabled={phase === 'uploading'}
          >
            选择整个文件夹
          </button>
          一次导入一个目录。
        </p>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={ACCEPT}
          style={{ display: 'none' }}
          onChange={(event) => {
            addFiles(event.target.files);
            event.target.value = '';
          }}
        />
        <input
          ref={dirRef}
          type="file"
          multiple
          style={{ display: 'none' }}
          {...{ webkitdirectory: '', directory: '' }}
          onChange={(event) => {
            addFiles(event.target.files);
            event.target.value = '';
          }}
        />
        <p className="help" style={{ margin: 0 }}>
          网页只读取你在浏览器里选中的文件；目录选择也只使用浏览器实际给出的文件集合，不会访问电脑上的其它路径。
        </p>
      </div>

      {files.length ? (
        <ul className="file-list">
          {files.map((file, index) => {
            const tooBig = file.size > MAX_BYTES;
            return (
              <li key={`${file.name}-${file.size}-${index}`}>
                <span className="f-name">{file.name}</span>
                <span className="num" style={{ color: 'var(--text-3)' }}>
                  {formatBytes(file.size)}
                </span>
                {tooBig ? <Tag tone="error">超过 1 GiB，本机服务会拒绝</Tag> : null}
                <button
                  type="button"
                  className="btn btn-sm btn-quiet"
                  disabled={phase === 'uploading'}
                  onClick={() => setFiles((current) => current.filter((_, position) => position !== index))}
                >
                  移除
                </button>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="help">还没有选择文件。PDF 会优先走识别流程，Word 文档按段落与表格建立文本单元。</p>
      )}

      {oversize.length ? (
        <div className="conflict-box" role="alert" style={{ marginTop: 12 }}>
          有 {oversize.length} 个文件超过 1 GiB，本机服务会拒绝接收。请移除后再上传。
        </div>
      ) : null}

      <fieldset style={{ border: 0, padding: 0, margin: '14px 0 0' }}>
        <legend className="lbl">识别方式</legend>
        {OCR_MODES.map((item) => (
          <label className="radio-row" key={item.value}>
            <input
              type="radio"
              name="ocr-mode"
              checked={ocrMode === item.value}
              disabled={phase === 'uploading'}
              onChange={() => setOcrMode(item.value)}
            />
            <span>
              {item.label}
              <span className="help" style={{ margin: 0 }}>
                {item.hint}
              </span>
            </span>
          </label>
        ))}
      </fieldset>

      {phase === 'uploading' ? (
        <section style={{ marginTop: 14 }} aria-live="polite">
          <p style={{ fontSize: 13 }}>
            上传到本机：{formatBytes(progress.loaded)}
            {progress.total ? ` / ${formatBytes(progress.total)}` : ''}
            {percent !== null ? `（${percent}%）` : ''}
          </p>
          <div className="progress-track">
            <i style={{ width: `${percent ?? 100}%`, opacity: percent === null ? 0.4 : 1 }} />
          </div>
          <p className="help" style={{ margin: 0 }}>
            这一段只表示文件字节传到本机服务；识别进度在上传结束后由后台任务给出，不会混在同一进度条里。
          </p>
        </section>
      ) : null}

      {phase === 'done' && result ? (
        <section style={{ marginTop: 14 }}>
          <div className="notice-box" role="status">
            已接收 <b>{result.job_ids.length}</b> 个文件，识别任务在后台排队。上传完成不等于识别完成，请在「识别任务」页查看真实进度。
          </div>
          {result.rejected.length ? (
            <>
              <p className="lbl" style={{ marginTop: 10 }}>
                被本机服务拒绝的文件（{result.rejected.length}）
              </p>
              <ul className="file-list">
                {result.rejected.map((item) => (
                  <li key={item.filename}>
                    <span className="f-name">{item.filename}</span>
                    <span className="error-text">{item.error}</span>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </section>
      ) : null}
    </Modal>
  );
}