import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { SearchMode } from '../api/types';
import { ResultList } from '../components/ResultList';
import { Tag } from '../components/Tag';
import { IconSearch } from '../components/Icons';
import { PageHeader } from '../components/PageHeader';
import { useDocuments } from '../hooks/useLibraryData';
import { useSearchResults } from '../hooks/useSearchResults';
import { summarizeCoverage } from '../lib/coverage';
import { num } from '../lib/format';
import { useAppState } from '../state/AppState';
import { Reader } from './Reader';

const MODE_OPTIONS: Array<{ value: SearchMode; label: string; hint: string }> = [
  { value: 'phrase', label: '精确短语', hint: '字面匹配整串词语，保留原文措辞。' },
  { value: 'all_terms', label: '全部包含', hint: '每个词都要出现在同一页中。' },
  { value: 'any_terms', label: '任一关键词', hint: '页面包含其中任一词即命中。' },
];

const STORAGE_KEY = 'litworkbench.search.v1';

interface StoredSearch {
  query: string;
  mode: SearchMode;
}

function readStored(): StoredSearch {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<StoredSearch>;
      const mode = parsed.mode === 'all_terms' || parsed.mode === 'any_terms' ? parsed.mode : 'phrase';
      return { query: typeof parsed.query === 'string' ? parsed.query : '', mode };
    }
  } catch {
    /* 忽略存储异常 */
  }
  return { query: '', mode: 'phrase' };
}

export function SearchPage() {
  const { coverage, refreshCoverage, applyCoverage, readerTarget, docScopeTarget, navigate } = useAppState();
  const initial = useRef(readStored());
  const [inputValue, setInputValue] = useState(initial.current.query);
  const [query, setQuery] = useState(initial.current.query);
  const [mode, setMode] = useState<SearchMode>(initial.current.mode);
  const [docIds, setDocIds] = useState<string[]>([]);
  const [selectedUnitId, setSelectedUnitId] = useState<string | null>(null);
  const [pane, setPane] = useState<'list' | 'detail'>('list');

  const documents = useDocuments(1);
  const search = useSearchResults({ query, mode, docIds, enabled: query.trim().length > 0 });
  const reloadSearch = search.reload;

  useEffect(() => {
    void refreshCoverage();
  }, [refreshCoverage]);

  useEffect(() => {
    if (search.response?.coverage) applyCoverage(search.response.coverage);
  }, [search.response, applyCoverage]);

  useEffect(() => {
    try {
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ query, mode }));
    } catch {
      /* 忽略 */
    }
  }, [mode, query]);

  // 摘录集「返回来源」：切到检索页并打开指定单元。
  useEffect(() => {
    if (!readerTarget) return;
    setSelectedUnitId(readerTarget.unitId);
    setPane('detail');
  }, [readerTarget]);

  // 文献库「在检索中查看」：带来源过滤条件进入检索页。
  useEffect(() => {
    if (!docScopeTarget) return;
    setDocIds([docScopeTarget.unitId]);
    setSelectedUnitId(null);
    setPane('list');
    reloadSearch();
  }, [docScopeTarget, reloadSearch]);

  const submit = useCallback(
    (nextQuery: string) => {
      setSelectedUnitId(null);
      setPane('list');
      setQuery(nextQuery);
      if (nextQuery.trim().length > 0) search.reload();
    },
    [search],
  );

  const hits = search.response?.items ?? [];
  const libraryEmpty = documents.data !== null && documents.data.total === 0;
  const summary = useMemo(() => summarizeCoverage(search.response?.coverage ?? coverage), [search.response, coverage]);
  const activeMode = MODE_OPTIONS.find((option) => option.value === mode) ?? MODE_OPTIONS[0];

  const selectUnit = (unitId: string) => {
    setSelectedUnitId(unitId);
    setPane('detail');
  };

  return (
    <div className="page">
      <PageHeader
        compact
        icon={<IconSearch size={30} />}
        title="全库检索"
        description="在已建立索引的文献文字中查找原文，回原页核对后保存摘录。只做字面匹配。"
      />

      <div className="search-row">
        <label className="sr" htmlFor="global-search">
          检索词
        </label>
        <div className="sfield">
          <span aria-hidden="true" style={{ color: 'var(--text-3)' }}>
            <IconSearch size={16} />
          </span>
          <input
            id="global-search"
            type="search"
            value={inputValue}
            placeholder="输入词、短语，例如：文庙"
            autoComplete="off"
            onChange={(event) => setInputValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                submit(inputValue);
              }
            }}
          />
        </div>
        <select
          className="qselect"
          aria-label="检索方式"
          value={mode}
          onChange={(event) => {
            const next = event.target.value as SearchMode;
            setMode(next);
            if (query.trim()) search.reload();
          }}
        >
          {MODE_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              方式：{option.label}
            </option>
          ))}
        </select>
        <button type="button" className="btn btn-primary" onClick={() => submit(inputValue)}>
          搜索
        </button>
        <span className="s-hint">{activeMode.hint}</span>
      </div>

      <div className="coverage-line">
        <span>{summary.text}</span>
        {summary.hasBacklog ? (
          <Tag tone="warn">待处理与失败页尚未计入覆盖</Tag>
        ) : null}
        {coverage?.unknown_documents ? <Tag tone="neutral">页数未知的文献不会生成假百分比</Tag> : null}
      </div>

      {search.notice ? (
        <div className="search-notice">
          <div className="notice-box" role="status">
            {search.notice}
            <div className="btn-row" style={{ marginTop: 6 }}>
              <button type="button" className="btn btn-sm" onClick={search.reload}>
                重新检索
              </button>
              <button type="button" className="btn btn-sm btn-quiet" onClick={search.clearNotice}>
                知道了
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <div className="viewbar">
        <span className="vtab" aria-selected="true">
          检索结果
        </span>
        <span className="vcount num">
          {search.response
            ? `${num(search.response.total)} 条命中 · ${num(search.response.total_documents)} 份文献 · 第 ${num(search.page)}/${num(
                search.totalPages,
              )} 页`
            : '尚未检索'}
        </span>
        <div className="vfilters">
          <label className="sr" htmlFor="scope-filter">
            来源范围
          </label>
          <select
            id="scope-filter"
            className="qselect"
            value={docIds[0] ?? 'all'}
            onChange={(event) => {
              const value = event.target.value;
              setDocIds(value === 'all' ? [] : [value]);
              setSelectedUnitId(null);
              search.reload();
            }}
          >
            <option value="all">来源：全部文献</option>
            {(documents.data?.items ?? []).map((doc) => (
              <option key={doc.id} value={doc.id}>
                {doc.title || doc.filename}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn btn-sm btn-quiet"
            onClick={() => {
              setInputValue('');
              setDocIds([]);
              setSelectedUnitId(null);
              setQuery('');
              search.clearNotice();
            }}
          >
            重置
          </button>
        </div>
      </div>

      <div className="split" data-pane={pane}>
        <section className="results" aria-label="检索结果">
          <div className="res-head">
            <span>{query.trim() ? `“${query.trim()}”` : '全库'}</span>
            <span aria-hidden="true">·</span>
            <span>{MODE_OPTIONS.find((option) => option.value === mode)?.label}</span>
            {search.loading ? <span className="nav-badge">检索中…</span> : null}
          </div>

          {libraryEmpty && !query.trim() ? (
            <div className="empty">
              <strong>文献库还是空的</strong>
              第一步：在文献库批量导入 PDF 或 DOCX。文件只上传到这台电脑上的本地服务，不会发送到任何第三方。
              <div className="empty-actions">
                <button type="button" className="btn btn-sm btn-primary" onClick={() => navigate('library')}>
                  去导入文献
                </button>
                <button type="button" className="btn btn-sm" onClick={() => navigate('jobs')}>
                  查看识别任务
                </button>
              </div>
              <p className="help">导入后，本机服务会在后台逐页提取或识别文字，完成即可全库检索。</p>
            </div>
          ) : search.error ? (
            <div className="empty">
              <strong>检索未完成</strong>
              {search.error.message}
              <div className="empty-actions">
                <button type="button" className="btn btn-sm" onClick={search.reload}>
                  重新检索
                </button>
              </div>
            </div>
          ) : hits.length ? (
            <ResultList
              hits={hits}
              selectedId={selectedUnitId}
              onSelect={selectUnit}
              query={query}
              mode={mode}
              loading={search.loading}
            />
          ) : query.trim() ? (
            <div className="empty">
              <strong>没有匹配的页面</strong>
              当前条件：<b>{query.trim()}</b>（{MODE_OPTIONS.find((option) => option.value === mode)?.label}）
              <br />
              {summary.text}
              <div className="empty-actions">
                <button type="button" className="btn btn-sm" onClick={() => submit(inputValue.replace(/\s+/g, ' '))}>
                  换词重试
                </button>
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => {
                    setMode(mode === 'phrase' ? 'all_terms' : 'phrase');
                    search.reload();
                  }}
                >
                  切换为{mode === 'phrase' ? '全部包含' : '精确短语'}
                </button>
                <button type="button" className="btn btn-sm" onClick={() => navigate('jobs')}>
                  查看待识别页
                </button>
              </div>
            </div>
          ) : (
            <div className="empty">
              <strong>输入词开始检索</strong>
              支持单字、双字、短语与标点；只检索已建立索引的文字页。
            </div>
          )}

          <div className="res-foot">
            <span>文件页 = 文件内页序；印刷页 = 书上印的页码，二者分开记录。</span>
            {search.totalPages > 1 ? (
              <span className="res-pager">
                <button type="button" className="btn btn-sm" disabled={search.page <= 1} onClick={() => search.setPage(search.page - 1)}>
                  上一页
                </button>
                <span className="num">
                  {num(search.page)} / {num(search.totalPages)}
                </span>
                <button
                  type="button"
                  className="btn btn-sm"
                  disabled={search.page >= search.totalPages}
                  onClick={() => search.setPage(search.page + 1)}
                >
                  下一页
                </button>
              </span>
            ) : null}
          </div>
        </section>

        <Reader
          unitId={selectedUnitId}
          onSelectUnit={selectUnit}
          onBack={() => setPane('list')}
          emptyHint={
            libraryEmpty
              ? '文献库还没有内容。先在文献库批量导入 PDF 或 DOCX，识别完成后即可检索与核对。'
              : '在左侧结果中选择一页，即可查看原件、识别文字与对照。'
          }
        />
      </div>
    </div>
  );
}