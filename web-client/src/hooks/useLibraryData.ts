/** 文献、任务、单元、摘录的读取钩子。只保存列表摘要，不把整页文字复制进 React。 */
import { useMemo } from 'react';
import { listCollections, listDocuments, listEvidence, listJobs, listUnits, getUnit } from '../api/endpoints';
import type { Collection, DocumentItem, EvidenceItem, JobItem, UnitDetail, UnitSummary } from '../api/types';
import type { Page } from '../api/types';
import { useAppState } from '../state/AppState';
import { isActiveJob } from '../lib/format';
import { useApiQuery } from './useApiQuery';
import type { QueryState } from './useApiQuery';

export type DocumentsQuery = QueryState<Page<DocumentItem>> & { page: number; totalPages: number };

export function useDocuments(page = 1, poll = false): DocumentsQuery {
  const { dataVersion } = useAppState();
  const query = useApiQuery<Page<DocumentItem>>(
    `documents:${page}:${dataVersion}`,
    (signal) => listDocuments(page, 30, signal),
    { poll },
  );
  const totalPages = query.data ? Math.max(1, Math.ceil(query.data.total / Math.max(1, query.data.page_size))) : 1;
  return { ...query, page, totalPages };
}

export type JobsQuery = QueryState<Page<JobItem>> & { activeCount: number; page: number };

export function useJobs(page = 1): JobsQuery {
  const { dataVersion } = useAppState();
  const query = useApiQuery<Page<JobItem>>(`jobs:${page}:${dataVersion}`, (signal) => listJobs(page, 30, signal));
  const activeCount = useMemo(
    () => (query.data?.items ?? []).filter((job) => isActiveJob(job.state)).length,
    [query.data],
  );
  return { ...query, page, activeCount };
}

export function useDocumentUnits(docId: string | null, page = 1): QueryState<Page<UnitSummary>> {
  const { dataVersion } = useAppState();
  return useApiQuery<Page<UnitSummary>>(
    docId ? `units:${docId}:${page}:${dataVersion}` : 'units:none',
    (signal) => listUnits(docId as string, page, 30, signal),
    { enabled: Boolean(docId) },
  );
}

export function useUnit(unitId: string | null): QueryState<UnitDetail> {
  return useApiQuery<UnitDetail>(
    unitId ? `unit:${unitId}` : 'unit:none',
    (signal) => getUnit(unitId as string, signal),
    { enabled: Boolean(unitId) },
  );
}

export interface EvidenceFilters {
  docId?: string;
  reviewStatus?: string;
  category?: string;
  collectionId?: string;
}

export interface EvidenceQuery extends QueryState<Page<EvidenceItem>> {
  page: number;
  totalPages: number;
}

export function useEvidenceList(page: number, filters: EvidenceFilters): EvidenceQuery {
  const { dataVersion } = useAppState();
  const docId = filters.docId ?? '';
  const status = filters.reviewStatus ?? '';
  const category = filters.category ?? '';
  const collectionId = filters.collectionId ?? '';
  const query = useApiQuery<Page<EvidenceItem>>(
    `evidence:${page}:${docId}:${status}:${category}:${collectionId}:${dataVersion}`,
    (signal) => listEvidence(
      { page, pageSize: 30, docId, reviewStatus: status, category, collectionId }, signal),
  );
  const totalPages = query.data ? Math.max(1, Math.ceil(query.data.total / Math.max(1, query.data.page_size))) : 1;
  return { ...query, page, totalPages };
}

/** 摘录集合。集合没有删除接口，页面因此也不提供删除按钮。 */
export function useCollections(): QueryState<Collection[]> {
  const { dataVersion } = useAppState();
  return useApiQuery<Collection[]>(`collections:${dataVersion}`, (signal) => listCollections(signal));
}