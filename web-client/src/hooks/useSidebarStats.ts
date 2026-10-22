/** 侧栏导航需要的真实计数：文献总数、活动任务数、摘录总数。没有数据就不显示徽标。 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { listDocuments, listEvidence, listJobs } from '../api/endpoints';
import type { DocumentItem, EvidenceItem, JobItem } from '../api/types';
import type { Page } from '../api/types';
import { isActiveJob } from '../lib/format';
import { useAppState } from '../state/AppState';
import { useApiQuery } from './useApiQuery';
import { usePolling } from './usePolling';

export interface SidebarStats {
  documentsTotal: number | null;
  jobsTotal: number | null;
  jobsActive: number;
  excerptsTotal: number | null;
}

/**
 * 文献计数与任务状态一起刷新：识别进行中文献总数、待处理页和覆盖统计都来自服务端，
 * 不在前端合成。只有存在活动任务时才轮询，任务全部结束后再收尾刷新一次。
 */
export function useSidebarStats(): SidebarStats {
  const { dataVersion, refreshCoverage } = useAppState();
  const [tick, setTick] = useState(0);

  const documents = useApiQuery<Page<DocumentItem>>(`stats-documents:${dataVersion}:${tick}`, (signal) =>
    listDocuments(1, 1, signal),
  );
  const jobs = useApiQuery<Page<JobItem>>(`stats-jobs:${dataVersion}:${tick}`, (signal) => listJobs(1, 30, signal));
  const evidence = useApiQuery<Page<EvidenceItem>>(`stats-evidence:${dataVersion}`, (signal) =>
    listEvidence({ page: 1, pageSize: 1 }, signal),
  );

  const activeJobs = useMemo(
    () => (jobs.data?.items ?? []).filter((job) => isActiveJob(job.state)).length,
    [jobs.data],
  );
  const hasActive = activeJobs > 0;

  usePolling(
    () => {
      setTick((value) => value + 1);
      void refreshCoverage();
    },
    { enabled: hasActive, foregroundMs: 5_000, backgroundMs: 15_000 },
  );

  // 最后一个活动任务结束时再取一次，避免停在“进行中”的旧计数上。
  const wasActive = useRef(false);
  useEffect(() => {
    if (activeJobs > 0) {
      wasActive.current = true;
      return;
    }
    if (!wasActive.current) return;
    wasActive.current = false;
    setTick((value) => value + 1);
    void refreshCoverage();
  }, [activeJobs, refreshCoverage]);

  return {
    documentsTotal: documents.data?.total ?? null,
    jobsTotal: jobs.data?.total ?? null,
    jobsActive: activeJobs,
    excerptsTotal: evidence.data?.total ?? null,
  };
}