import { memo } from 'react';
import type { SearchHit } from '../api/types';
import type { SearchMode } from '../api/types';
import { segmentByTerms, queryTerms } from '../lib/highlight';
import { filetypeLabel, isCorrectedVersion, num, unitStatusInfo, versionLabel } from '../lib/format';
import { Tag } from './Tag';

interface ResultListProps {
  hits: SearchHit[];
  selectedId: string | null;
  onSelect: (unitId: string) => void;
  query: string;
  mode: SearchMode;
  loading: boolean;
}

/**
 * 命中列表：片段是服务端返回的原文，始终按纯文本节点渲染，
 * 命中词只用 <mark> 标注，不解析任何 HTML。
 */
export const ResultList = memo(function ResultList({ hits, selectedId, onSelect, query, mode, loading }: ResultListProps) {
  const terms = queryTerms(query, mode);
  return (
    <ul className="res-list" aria-label="检索结果列表" aria-busy={loading}>
      {hits.map((hit) => (
        <li key={`${hit.unit_id}-${hit.start_offset}-${hit.end_offset}`}>
          <button
            type="button"
            className="result"
            aria-current={hit.unit_id === selectedId}
            onClick={() => onSelect(hit.unit_id)}
          >
            <span className="r-title">{hit.title || hit.filename}</span>
            {hit.title_path ? <span className="r-path">{hit.title_path}</span> : null}
            <span className="r-snip">
              {segmentByTerms(hit.snippet, terms).map((segment, index) =>
                segment.hit ? (
                  <mark key={index}>{segment.text}</mark>
                ) : (
                  <span key={index}>{segment.text}</span>
                ),
              )}
            </span>
            <span className="r-meta">
              <Tag tone="neutral">{filetypeLabel(hit.kind)}</Tag>
              <span className="num">
                {hit.file_page === null ? '文档单元' : `文件第 ${num(hit.file_page)} 页`}
                {hit.printed_page ? ` · 印刷页 ${hit.printed_page}` : ''}
              </span>
              {isCorrectedVersion(hit.version) ? <Tag tone="info">{versionLabel(hit.version)}</Tag> : null}
              <Tag tone={unitStatusInfo(hit.status).tone} state>
                {unitStatusInfo(hit.status).label}
              </Tag>
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
});