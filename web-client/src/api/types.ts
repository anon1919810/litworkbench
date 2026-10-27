/**
 * 与 docs/web-milestone-1-contract.md 一一对应的类型。
 * 只声明契约承诺的字段；后端若附带额外字段，前端不使用。
 */

/** 分页信封：默认 page_size=30，最大 100。 */
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/**
 * 覆盖对象。已覆盖 = 当前版已索引文字页 + 人工确认空白页。
 * 页数未知时为 null，不生成假百分比。
 */
export interface Coverage {
  known_pdf_pages: number | null;
  covered_pdf_pages: number | null;
  pending_pdf_pages: number | null;
  failed_pdf_pages: number | null;
  unknown_documents: number | null;
  indexed_docx: number | null;
  total_docx: number | null;
}

export interface OcrAvailability {
  available: boolean;
  detail?: string | null;
}

/** GET /workspace 的返回；同时建立会话 cookie。csrf_token 只在内存中保存。 */
export interface WorkspaceInfo {
  name: string;
  root?: string;
  mode?: 'local' | 'cloud';
  worker_enabled?: boolean;
  csrf_token: string;
  ocr: OcrAvailability;
  coverage: Coverage;
}

export type Filetype = 'pdf' | 'docx' | (string & {});

/** GET /documents 的条目。page_count 可为 null（页数尚未统计）。 */
export interface DocumentItem {
  id: string;
  title: string | null;
  filename: string;
  filetype: Filetype;
  page_count: number | null;
  unit_count: number;
  status: string;
  pending_count: number;
  searchable_pages: number;
  author: string | null;
  year: string | null;
  edition: string | null;
  volume: string | null;
  ocr_mode?: OcrMode | null;
  imported_at?: string | null;
  updated_at?: string | null;
}

/** PATCH /documents/{id} 仅可编辑书目字段。 */
export interface DocumentBibPatch {
  title?: string | null;
  author?: string | null;
  year?: string | null;
  edition?: string | null;
  volume?: string | null;
}

/**
 * 印刷页码是字符串：可能是 “775”、罗马数字或中文标注。
 * 空字符串表示尚未填写；前端不把它当数字解析。
 */
export type PrintedPage = string | null;

/** GET /documents/{id}/units 的简要单元，供逐页跳转。DOCX 的 file_page 为 null。 */
export interface UnitSummary {
  id: string;
  doc_id: string;
  seq: number;
  kind: string;
  file_page: number | null;
  printed_page: PrintedPage;
  printed_page_status?: PrintedPageStatus | null;
  status: string;
  method: string | null;
  title_path: string | null;
}

/** 印刷页码状态：不由文件页序自动推算。 */
export type PrintedPageStatus = 'candidate' | 'confirmed' | 'none' | (string & {});

/** 文本版本：original = 识别原文，corrected = 人工校订后的文本。 */
export type TextVersion = 'original' | 'corrected' | (string & {});

/** OCR 行框：核心结构为 box 多边形 [[x, y] × 4]；个别实现可能给出 bbox。 */
export interface OcrLine {
  text?: string | null;
  box?: Array<[number, number]> | null;
  bbox?: [number, number, number, number] | null;
  confidence?: number | null;
  [extra: string]: unknown;
}

export interface OcrInfo {
  engine?: string | null;
  lines?: OcrLine[] | null;
  mean_confidence?: number | null;
  render_width?: number | null;
  render_height?: number | null;
  [extra: string]: unknown;
}

/** 校订 / 重新识别留下的历史。 */
export interface UnitRevision {
  at: string | null;
  kind: string;
  reason: string | null;
}

/** GET /units/{id} 的返回。 */
export interface UnitDetail {
  unit_id: string;
  doc_id: string;
  title: string | null;
  filename: string;
  kind: string;
  seq: number;
  file_page: number | null;
  printed_page: PrintedPage;
  printed_page_status: PrintedPageStatus | null;
  title_path: string | null;
  method: string | null;
  status: string;
  notes: string | null;
  text: string;
  original_text: string | null;
  corrected_text: string | null;
  /** 'original' | 'corrected'，表示当前 text 取自哪个版本 */
  version: TextVersion;
  text_sha256: string;
  /**
   * 页内容修订号：机器 OCR、人工校订、确认空白都会推进；仅改书目/印刷页码不推进。
   * 旧 fixture 可能没有该字段，此时不能臆造 0，也不允许采用候选。
   */
  content_revision?: number;
  ocr: OcrInfo | null;
  revisions: UnitRevision[];
}

export type OcrMode = 'auto' | 'off' | 'force';

export interface ImportRejection {
  filename: string;
  error: string;
}

/** POST /imports：202；全部被拒时后端返回 400。 */
export interface ImportResult {
  job_ids: string[];
  rejected: ImportRejection[];
}

export type JobState =
  | 'queued'
  | 'preparing'
  | 'running'
  | 'pausing'
  | 'paused'
  | 'partial'
  | 'completed'
  | 'failed'
  | (string & {});

/** GET /jobs 的条目。doc_id / total_pages 可为 null。 */
export interface JobItem {
  id: string;
  filename: string;
  doc_id: string | null;
  kind: string;
  state: JobState;
  stage: string | null;
  total_pages: number | null;
  processed_pages: number;
  completed_pages: number;
  failed_pages: number;
  pending_pages: number;
  searchable_pages: number;
  duplicate: boolean;
  error: string | null;
  pause_requested: boolean;
  /** 候选任务专用：只计 ready 候选的待核对页数（推理完成页数不等于已人工采用）。 */
  review_pending_pages?: number;
  created_at: string | null;
  updated_at: string | null;
}

/** GET /jobs/{id}/units 的逐页检查点：错误原因与尝试次数都来自服务端。 */
export interface JobUnitItem {
  unit_id: string;
  position: number;
  file_page?: number | null;
  state: string;
  attempts: number;
  error: string | null;
  updated_at: string | null;
  /** 候选任务可选：该页最近候选及其状态，供待核导航。 */
  candidate_id?: string | null;
  candidate_state?: OcrCandidateState | null;
}

export type SearchMode = 'phrase' | 'all_terms' | 'any_terms';

export interface SearchRequest {
  query: string;
  mode: SearchMode;
  doc_ids?: string[];
  page: number;
  page_size: number;
  expected_index_revision?: number;
}

/** POST /search 的单条命中。offset 为 Unicode 码点偏移。 */
export interface SearchHit {
  unit_id: string;
  doc_id: string;
  title: string | null;
  filename: string;
  kind: string;
  file_page: number | null;
  printed_page: PrintedPage;
  title_path: string | null;
  method: string | null;
  status: string;
  snippet: string;
  start_offset: number;
  end_offset: number;
  text_sha256: string;
  version: TextVersion;
  filetype?: Filetype;
  seq?: number;
}

/** POST /search 的返回：分页 + 覆盖 + 索引代次 + 命中文献数。 */
export interface SearchResponse extends Page<SearchHit> {
  coverage: Coverage;
  index_revision: number;
  total_documents: number;
}

export type ReviewStatus = 'pending' | 'accepted' | 'rejected' | 'stale';

/** POST /evidence 的请求。start_offset 为 Unicode 码点偏移。 */
export interface EvidenceCreate {
  unit_id: string;
  quote: string;
  start_offset: number;
  expected_hash: string;
  category: string;
  note: string;
}

export interface EvidenceSource {
  filename: string;
  title: string | null;
  file_page: number | null;
  printed_page: PrintedPage;
  printed_page_status?: PrintedPageStatus | null;
  title_path: string | null;
  kind?: string;
  seq?: number;
  author?: string | null;
  year?: string | null;
  unit_text_sha256?: string;
  /** 服务端给出的可读定位串 */
  locator?: string;
}

export interface EvidenceItem {
  id: string;
  unit_id: string;
  doc_id: string;
  quote: string;
  start_offset: number;
  end_offset: number;
  version: TextVersion;
  category: string | null;
  note: string | null;
  review_status: ReviewStatus;
  review_note: string | null;
  created_at: string | null;
  source: EvidenceSource;
}

export interface EvidencePatch {
  review_status?: ReviewStatus;
  note?: string;
  review_note?: string;
}

/* ---------------------------------- 集合 ---------------------------------- */

/** 摘录集合（专题）。集合没有删除接口：成员可以移出，集合本身长期保留。 */
export interface Collection {
  id: string;
  name: string;
  description: string;
  evidence_count: number;
  created_at: string;
  updated_at: string;
}

export interface CollectionCreate {
  name: string;
  description?: string;
}

export interface CollectionPatch {
  name?: string;
  description?: string;
}

/** POST /evidence/batch：只处理显式 ID，核验状态不参与批量修改。 */
export interface EvidenceBatchRequest {
  evidence_ids: string[];
  category?: string;
  add_to_collection_id?: string;
  remove_from_collection_id?: string;
}

export interface EvidenceBatchResult {
  evidence_ids: string[];
  updated: number;
  collection_id: string | null;
  added: number;
  removed: number;
}

/* ---------------------------------- 导出 ---------------------------------- */

export type ExportFormat = 'csv' | 'json' | 'markdown' | 'docx';

/** 导出范围：各条件取交集；未指定 review_status 时服务端只取 accepted。 */
export interface ExportFilters {
  doc_id?: string;
  category?: string;
  review_status?: string;
  collection_id?: string;
}

export interface ExportRequest {
  format: ExportFormat;
  filters: ExportFilters;
  /** 只有「已选」模式才出现；空数组会被服务端拒绝，前端也不发。 */
  evidence_ids?: string[];
}

export interface ExportPreview extends ExportRequest {
  count: number;
  review_counts: Record<ReviewStatus, number>;
  snapshot_token: string;
  filename: string;
  scope_description: string;
  /** 'accepted'（默认）或 'all' / 具体状态 */
  review_policy: string;
  material_note: string;
  review_note: string;
}

export interface ExportRecord {
  id: string;
  format: ExportFormat;
  filename: string;
  count: number;
  review_counts: Record<ReviewStatus, number>;
  download_url: string;
  sha256: string;
  byte_size: number;
  snapshot_token: string;
  scope: ExportFilters;
  created_at: string;
}

export interface UnitCorrection {
  text: string;
  reason: string;
  expected_hash: string;
}

export interface PrintedPagePatch {
  /** 字符串：可能是 “775”、罗马数字或中文标注；不能为空 */
  printed_page: string;
  status: 'candidate' | 'confirmed';
}

export interface ReprocessRequest {
  unit_ids: string[];
  mode: 'force';
  keep_corrections: true;
}

export interface ReprocessResult {
  job_ids: string[];
}

/* ------------------------------- OCR 候选 ------------------------------- */

/** GET /ocr/profiles 的条目：稳定配置与真实可用性；不回显绝对模型路径。 */
export interface OcrProfile {
  id: string;
  label: string;
  description?: string | null;
  available: boolean;
  reason?: string | null;
}

export interface OcrProfileList {
  items: OcrProfile[];
}

export type OcrCandidateState =
  | 'queued'
  | 'running'
  | 'ready'
  | 'failed'
  | 'adopted'
  | 'rejected'
  | 'stale'
  | (string & {});

export interface OcrCandidateError {
  kind: string;
  message: string;
}

/** 候选识别结果；没有框/置信度时为 null，不伪造。 */
export interface OcrCandidateResult {
  text: string;
  lines: OcrLine[];
  engine?: string | null;
  model_fingerprints?: unknown[];
  config?: Record<string, unknown>;
  timing?: Record<string, unknown>;
  memory?: Record<string, unknown>;
  coordinate_system?: string | null;
  render_width?: number | null;
  render_height?: number | null;
  [extra: string]: unknown;
}

/** GET /units/{id}/ocr-candidates 列表项：候选摘要，可省略 result/error（选中后再取详情）。 */
export interface OcrCandidateSummary {
  id: string;
  job_id: string;
  attempt_id: string;
  unit_id: string;
  doc_id: string;
  file_page: number | null;
  state: OcrCandidateState;
  base_content_revision: number;
  profile_id: string;
  layout: string;
  /** 响应已含完整结果时直接使用；省略（undefined）表示需要用详情接口补齐。 */
  result?: OcrCandidateResult | null;
  error?: OcrCandidateError | null;
  created_at: string | null;
  decided_at: string | null;
}

/** GET /ocr-candidates/{id} 详情，以及 adopt/reject 返回：result/error 必到但可为 null。 */
export interface OcrCandidateDetail extends OcrCandidateSummary {
  result: OcrCandidateResult | null;
  error: OcrCandidateError | null;
}

/** POST /documents/{doc_id}/ocr-candidate-jobs 请求。 */
export interface OcrCandidateJobRequest {
  unit_ids: string[];
  profile_id: string;
  layout: 'original';
}

/** POST /ocr-candidates/{id}/adopt 返回：候选与采用后的权威现用单元。 */
export interface OcrCandidateAdoptResult {
  candidate: OcrCandidateDetail;
  unit: UnitDetail;
}

/** 后端错误信封 {error:{code,message,details?}}。 */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: unknown;
  };
}
