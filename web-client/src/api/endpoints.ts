/**
 * 契约端点的类型化调用。所有 id 都经过 encodeURIComponent，路径只作为路径使用。
 */
import { apiAssetUrl, apiGet, apiPatch, apiPost, ApiError, peekCsrfToken, bootWorkspace, reauthenticate } from './client';
import type {
  Collection,
  CollectionCreate,
  CollectionPatch,
  DocumentBibPatch,
  DocumentItem,
  EvidenceBatchRequest,
  EvidenceBatchResult,
  EvidenceCreate,
  EvidenceItem,
  EvidencePatch,
  ExportPreview,
  ExportRecord,
  ExportRequest,
  ImportResult,
  JobItem,
  JobUnitItem,
  OcrCandidateAdoptResult,
  OcrCandidateDetail,
  OcrCandidateJobRequest,
  OcrCandidateSummary,
  OcrProfileList,
  Page,
  PrintedPagePatch,
  ReprocessRequest,
  ReprocessResult,
  SearchRequest,
  SearchResponse,
  UnitCorrection,
  UnitDetail,
  UnitSummary,
  WorkspaceInfo,
} from './types';

const id = (value: string): string => encodeURIComponent(value);

function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

/* ---------------------------------- 工作区 --------------------------------- */

export const getWorkspace = (): Promise<WorkspaceInfo> => bootWorkspace<WorkspaceInfo>();

export const getCoverage = (signal?: AbortSignal): Promise<{ coverage: import('./types').Coverage }> =>
  apiGet('/coverage', signal);

/* ---------------------------------- 文献 ---------------------------------- */

export async function listDocuments(page = 1, pageSize = 30, signal?: AbortSignal): Promise<Page<DocumentItem>> {
  return apiGet<Page<DocumentItem>>(`/documents${query({ page, page_size: pageSize })}`, signal);
}

export const getDocument = (docId: string, signal?: AbortSignal): Promise<DocumentItem> =>
  apiGet<DocumentItem>(`/documents/${id(docId)}`, signal);

export const patchDocument = (docId: string, body: DocumentBibPatch, signal?: AbortSignal): Promise<DocumentItem> =>
  apiPatch<DocumentItem>(`/documents/${id(docId)}`, body, signal);

export async function listUnits(
  docId: string,
  page = 1,
  pageSize = 30,
  signal?: AbortSignal,
): Promise<Page<UnitSummary>> {
  return apiGet<Page<UnitSummary>>(`/documents/${id(docId)}/units${query({ page, page_size: pageSize })}`, signal);
}

export const documentSourceUrl = (docId: string): string => apiAssetUrl(`/documents/${id(docId)}/source`);

/** 逐页取完整个文献的单元 id，供“整本强制重新识别”使用。只取 id，不复制文字。 */
export async function listAllUnitIds(docId: string, signal?: AbortSignal): Promise<string[]> {
  const first = await listUnits(docId, 1, 100, signal);
  const ids = first.items.map((item) => item.id);
  const pageSize = Math.max(1, first.page_size || 30);
  const pages = Math.ceil(first.total / pageSize);
  for (let page = 2; page <= pages; page += 1) {
    const next = await listUnits(docId, page, pageSize, signal);
    ids.push(...next.items.map((item) => item.id));
  }
  return ids;
}

export const reprocessDocument = (
  docId: string,
  body: ReprocessRequest,
  signal?: AbortSignal,
): Promise<ReprocessResult> => apiPost<ReprocessResult>(`/documents/${id(docId)}/reprocess`, body, signal);

/**
 * 逐页取完整个文献的单元摘要（含 file_page），供候选范围解析使用。
 * 只读取页序与单元身份，不复制整页文字。
 */
export async function listAllUnits(docId: string, signal?: AbortSignal): Promise<UnitSummary[]> {
  const pageSize = 100;
  const first = await listUnits(docId, 1, pageSize, signal);
  const items = [...first.items];
  const pages = Math.ceil(first.total / Math.max(1, first.page_size || pageSize));
  for (let page = 2; page <= pages; page += 1) {
    const next = await listUnits(docId, page, pageSize, signal);
    items.push(...next.items);
  }
  return items;
}

/* ---------------------------------- 单元 ---------------------------------- */

export const getUnit = (unitId: string, signal?: AbortSignal): Promise<UnitDetail> =>
  apiGet<UnitDetail>(`/units/${id(unitId)}`, signal);

/** 仅允许 scale = 1 | 1.5 | 2 | 3；DOCX 不请求渲染。 */
export const RENDER_SCALES = [1, 1.5, 2, 3] as const;
export type RenderScale = (typeof RENDER_SCALES)[number];

export function unitRenderUrl(unitId: string, scale: RenderScale): string {
  return apiAssetUrl(`/units/${id(unitId)}/render${query({ scale })}`);
}

export const patchUnitCorrection = (
  unitId: string,
  body: UnitCorrection,
  signal?: AbortSignal,
): Promise<UnitDetail> => apiPatch<UnitDetail>(`/units/${id(unitId)}/correction`, body, signal);

export const patchUnitPrintedPage = (
  unitId: string,
  body: PrintedPagePatch,
  signal?: AbortSignal,
): Promise<UnitDetail> => apiPatch<UnitDetail>(`/units/${id(unitId)}/printed-page`, body, signal);

export const confirmUnitBlank = (unitId: string, signal?: AbortSignal): Promise<UnitDetail> =>
  apiPost<UnitDetail>(`/units/${id(unitId)}/confirm-blank`, {}, signal);

/* ---------------------------------- 检索 ---------------------------------- */

export function postSearch(body: SearchRequest, signal?: AbortSignal): Promise<SearchResponse> {
  return apiPost<SearchResponse>('/search', body, signal);
}

/* ---------------------------------- 摘录 ---------------------------------- */

export const createEvidence = (body: EvidenceCreate, signal?: AbortSignal): Promise<EvidenceItem> =>
  apiPost<EvidenceItem>('/evidence', body, signal);

export interface EvidenceListParams {
  page?: number;
  pageSize?: number;
  docId?: string;
  reviewStatus?: string;
  category?: string;
  collectionId?: string;
}

export function listEvidence(
  params: EvidenceListParams = {},
  signal?: AbortSignal,
): Promise<Page<EvidenceItem>> {
  return apiGet<Page<EvidenceItem>>(
    `/evidence${query({
      page: params.page,
      page_size: params.pageSize,
      doc_id: params.docId,
      review_status: params.reviewStatus,
      category: params.category,
      collection_id: params.collectionId,
    })}`,
    signal,
  );
}

export const patchEvidence = (idValue: string, body: EvidencePatch, signal?: AbortSignal): Promise<EvidenceItem> =>
  apiPatch<EvidenceItem>(`/evidence/${id(idValue)}`, body, signal);

/** 只发送显式 ID；空数组不会被发出（服务端会拒绝空选择）。 */
export function batchEvidence(body: EvidenceBatchRequest, signal?: AbortSignal): Promise<EvidenceBatchResult> {
  return apiPost<EvidenceBatchResult>('/evidence/batch', body, signal);
}

/* ---------------------------------- 集合 ---------------------------------- */

export async function listCollections(signal?: AbortSignal): Promise<Collection[]> {
  const page = await apiGet<Page<Collection>>('/collections', signal);
  return page.items;
}

export const createCollection = (body: CollectionCreate, signal?: AbortSignal): Promise<Collection> =>
  apiPost<Collection>('/collections', body, signal);

export const patchCollection = (
  collectionId: string,
  body: CollectionPatch,
  signal?: AbortSignal,
): Promise<Collection> => apiPatch<Collection>(`/collections/${id(collectionId)}`, body, signal);

/* ---------------------------------- 导出 ---------------------------------- */

/** 只预览、不写文件。返回的 snapshot_token 必须原样带回生成接口。 */
export const previewExport = (body: ExportRequest, signal?: AbortSignal): Promise<ExportPreview> =>
  apiPost<ExportPreview>('/exports/preview', body, signal);

export const createExport = (
  body: ExportRequest & { snapshot_token: string },
  signal?: AbortSignal,
): Promise<ExportRecord> => apiPost<ExportRecord>('/exports', body, signal);

/** 下载地址由服务端按 ID 解析；前端不拼接任何文件路径。 */
export const exportDownloadUrl = (exportId: string): string => apiAssetUrl(`/exports/${id(exportId)}/download`);

/* ---------------------------------- 任务 ---------------------------------- */

export async function listJobs(page = 1, pageSize = 30, signal?: AbortSignal): Promise<Page<JobItem>> {
  return apiGet<Page<JobItem>>(`/jobs${query({ page, page_size: pageSize })}`, signal);
}

export const getJob = (jobId: string, signal?: AbortSignal): Promise<JobItem> => apiGet<JobItem>(`/jobs/${id(jobId)}`, signal);

/** 任务逐页检查点：真实状态、尝试次数与失败原因（不是前端推算的）。 */
export function listJobUnits(
  jobId: string,
  page = 1,
  pageSize = 30,
  signal?: AbortSignal,
): Promise<Page<JobUnitItem>> {
  return apiGet<Page<JobUnitItem>>(`/jobs/${id(jobId)}/units${query({ page, page_size: pageSize })}`, signal);
}

export const pauseJob = (jobId: string, signal?: AbortSignal): Promise<JobItem> =>
  apiPost<JobItem>(`/jobs/${id(jobId)}/pause`, {}, signal);

export const resumeJob = (jobId: string, signal?: AbortSignal): Promise<JobItem> =>
  apiPost<JobItem>(`/jobs/${id(jobId)}/resume`, {}, signal);

export const retryJobFailed = (jobId: string, signal?: AbortSignal): Promise<JobItem> =>
  apiPost<JobItem>(`/jobs/${id(jobId)}/retry-failed`, {}, signal);

/* ------------------------------- OCR 候选 ------------------------------- */

/** GET /ocr/profiles：稳定配置与真实可用性（不运行 OCR）。 */
export const listOcrProfiles = (signal?: AbortSignal): Promise<OcrProfileList> =>
  apiGet<OcrProfileList>('/ocr/profiles', signal);

/** POST /documents/{doc_id}/ocr-candidate-jobs：只排队，返回既有 JobItem 信封。 */
export const createOcrCandidateJob = (
  docId: string,
  body: OcrCandidateJobRequest,
  signal?: AbortSignal,
): Promise<JobItem> => apiPost<JobItem>(`/documents/${id(docId)}/ocr-candidate-jobs`, body, signal);

export interface OcrCandidateListParams {
  page?: number;
  pageSize?: number;
  jobId?: string;
}

export function listOcrCandidates(
  unitId: string,
  params: OcrCandidateListParams = {},
  signal?: AbortSignal,
): Promise<Page<OcrCandidateSummary>> {
  return apiGet<Page<OcrCandidateSummary>>(
    `/units/${id(unitId)}/ocr-candidates${query({
      page: params.page,
      page_size: params.pageSize,
      job_id: params.jobId,
    })}`,
    signal,
  );
}

export const getOcrCandidate = (candidateId: string, signal?: AbortSignal): Promise<OcrCandidateDetail> =>
  apiGet<OcrCandidateDetail>(`/ocr-candidates/${id(candidateId)}`, signal);

/** expected_content_revision 必填；返回候选与采用后的权威现用单元。 */
export const adoptOcrCandidate = (
  candidateId: string,
  body: { expected_content_revision: number },
  signal?: AbortSignal,
): Promise<OcrCandidateAdoptResult> =>
  apiPost<OcrCandidateAdoptResult>(`/ocr-candidates/${id(candidateId)}/adopt`, body, signal);

export const rejectOcrCandidate = (
  candidateId: string,
  body: { reason?: string } = {},
  signal?: AbortSignal,
): Promise<OcrCandidateDetail> =>
  apiPost<OcrCandidateDetail>(`/ocr-candidates/${id(candidateId)}/reject`, { reason: '', ...body }, signal);

/* ---------------------------------- 导入 ---------------------------------- */

export interface ImportProgress {
  loaded: number;
  total: number | null;
}

export interface ImportUploadHandle {
  promise: Promise<ImportResult>;
  abort: () => void;
}

export interface ImportUploadOptions {
  files: File[];
  ocrMode: 'auto' | 'off' | 'force';
  onProgress: (progress: ImportProgress) => void;
  signal?: AbortSignal;
}

/**
 * 批量导入上传。multipart 重复字段 files + 字符串 ocr_mode。
 * 用 XMLHttpRequest 是为了拿到真实的“上传到本机”进度；OCR 进度由服务端任务状态给出，两者不混用。
 */
export function uploadImport(options: ImportUploadOptions): ImportUploadHandle {
  const xhr = new XMLHttpRequest();
  const onAbort = () => xhr.abort();
  options.signal?.addEventListener('abort', onAbort, { once: true });

  const promise = (async (): Promise<ImportResult> => {
    const token = peekCsrfToken();
    if (!token) throw new ApiError(0, 'NO_SESSION', '本地会话尚未建立，请刷新页面重试。');

    const form = new FormData();
    for (const file of options.files) form.append('files', file, file.name);
    form.append('ocr_mode', options.ocrMode);

    return await new Promise<ImportResult>((resolve, reject) => {
      xhr.open('POST', '/api/v1/imports', true);
      xhr.withCredentials = true;
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.setRequestHeader('X-CSRF-Token', token);
      xhr.responseType = 'text';
      xhr.upload.onprogress = (event) => {
        options.onProgress({
          loaded: event.loaded,
          total: event.lengthComputable && event.total > 0 ? event.total : null,
        });
      };
      xhr.onerror = () =>
        reject(new ApiError(0, 'NETWORK', '上传失败：无法连接本地文献服务，请确认启动器仍在运行。'));
      xhr.onabort = () => reject(new DOMException('上传已取消', 'AbortError'));
      xhr.onload = () => {
        let payload: unknown = null;
        try {
          payload = xhr.responseText ? JSON.parse(xhr.responseText) : null;
        } catch {
          payload = null;
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(payload as ImportResult);
          return;
        }
        const envelope = payload as { error?: { code?: string; message?: string; details?: unknown } } | null;
        const err = envelope?.error;
        reject(
          new ApiError(
            xhr.status,
            err?.code ?? `HTTP_${xhr.status}`,
            err?.message ?? `导入失败（HTTP ${xhr.status}）`,
            err?.details,
          ),
        );
      };
      xhr.send(form);
    });
  })().finally(() => {
    options.signal?.removeEventListener('abort', onAbort);
  });

  return { promise, abort: () => xhr.abort() };
}

/* ---------------------------------- 会话 ---------------------------------- */

/** 显式重新握手：丢弃旧令牌再取新的 csrf_token（服务重启后使用）。 */
export const refreshSession = (): Promise<unknown> => reauthenticate();