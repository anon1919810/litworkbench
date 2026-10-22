import { useMemo, useState } from 'react';
import type { DocumentItem } from '../api/types';
import { IconLibrary } from '../components/Icons';
import { PageHeader } from '../components/PageHeader';
import { Tag } from '../components/Tag';
import { useDocuments } from '../hooks/useLibraryData';
import { usePolling } from '../hooks/usePolling';
import { summarizeCoverage } from '../lib/coverage';
import { filetypeLabel, num, unitStatusInfo } from '../lib/format';
import { useAppState } from '../state/AppState';
import { BibDialog } from './BibDialog';
import { ImportDialog } from './ImportDialog';

export function LibraryPage() {
  const { coverage, refreshCounts, bumpDataVersion, openDocScope, navigate } = useAppState();
  const [page, setPage] = useState(1);
  const [importOpen, setImportOpen] = useState(false);
  const [editing, setEditing] = useState<DocumentItem | null>(null);

  const documents = useDocuments(page);
  const items = documents.data?.items ?? [];
  const hasBacklog = useMemo(() => items.some((doc) => doc.pending_count > 0), [items]);
  usePolling(documents.reload, { enabled: hasBacklog, foregroundMs: 5_000, backgroundMs: 15_000 });
  const summary = summarizeCoverage(coverage);
  const total = documents.data?.total ?? null;
  const pages = documents.totalPages;

  return (
    <div className="page">
      <PageHeader
        icon={<IconLibrary size={30} />}
        title="文献库"
        description="集中管理原件、书目信息与识别状态。导入的文件只保存在这台电脑上。"
      />
      <div className="page-body">
        <div className="db-toolbar">
          <span className="db-name">文献</span>
          <span className="db-count num">{total === null ? '统计中' : `${num(total)} 份`}</span>
          <span className="db-count">{summary.text}</span>
          <div className="db-right">
            <button type="button" className="btn btn-sm" onClick={refreshCounts}>
              刷新覆盖与计数
            </button>
            <button type="button" className="btn btn-sm" onClick={() => navigate('jobs')}>
              查看识别任务
            </button>
            <button type="button" className="btn btn-sm btn-primary" onClick={() => setImportOpen(true)}>
              批量导入
            </button>
          </div>
        </div>

        {documents.error ? (
          <div className="conflict-box" role="alert">
            {documents.error.message}
            <div className="btn-row" style={{ marginTop: 8 }}>
              <button type="button" className="btn btn-sm" onClick={documents.reload}>
                重试
              </button>
            </div>
          </div>
        ) : null}

        {total === 0 ? (
          <div className="empty">
            <strong>还没有导入任何文献</strong>
            批量选择 PDF 或 DOCX 上传到本机服务；识别在后台进行，完成后即可全库检索。
            <div className="empty-actions">
              <button type="button" className="btn btn-sm btn-primary" onClick={() => setImportOpen(true)}>
                批量导入
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="tbl-wrap">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>书名 / 文件名</th>
                    <th>类型</th>
                    <th className="n">页数</th>
                    <th className="n">单元</th>
                    <th className="n">已索引页</th>
                    <th className="n">待处理</th>
                    <th>状态</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {items.map((doc) => {
                    const status = unitStatusInfo(doc.status);
                    return (
                      <tr key={doc.id}>
                        <td>
                          <div style={{ fontWeight: 500 }}>{doc.title || '（未填写书名）'}</div>
                          <div style={{ color: 'var(--text-3)', fontSize: 12.5, overflowWrap: 'anywhere' }}>{doc.filename}</div>
                          {doc.author || doc.year || doc.edition || doc.volume ? (
                            <div style={{ color: 'var(--text-3)', fontSize: 12.5 }}>
                              {[doc.author, doc.year, doc.edition, doc.volume].filter(Boolean).join(' · ')}
                            </div>
                          ) : null}
                        </td>
                        <td>
                          <Tag tone="neutral">{filetypeLabel(doc.filetype)}</Tag>
                        </td>
                        <td className="n">{doc.page_count === null ? '统计中' : num(doc.page_count)}</td>
                        <td className="n">{num(doc.unit_count)}</td>
                        <td className="n">{num(doc.searchable_pages)}</td>
                        <td className="n">{doc.pending_count ? num(doc.pending_count) : '—'}</td>
                        <td>
                          <Tag tone={status.tone} state>
                            {status.label}
                          </Tag>
                        </td>
                        <td>
                          <div className="btn-row" style={{ justifyContent: 'flex-end' }}>
                            <button type="button" className="btn btn-sm" onClick={() => openDocScope(doc.id)}>
                              在检索中查看
                            </button>
                            <button type="button" className="btn btn-sm" onClick={() => setEditing(doc)}>
                              编辑书目
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {pages > 1 ? (
              <div className="res-pager" style={{ marginTop: 12 }}>
                <button type="button" className="btn btn-sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>
                  上一页
                </button>
                <span className="num">
                  {num(page)} / {num(pages)}
                </span>
                <button type="button" className="btn btn-sm" disabled={page >= pages} onClick={() => setPage(page + 1)}>
                  下一页
                </button>
              </div>
            ) : null}
          </>
        )}

        <p className="fine-print">
          <b>覆盖</b>指当前版本已建立索引的文字页，加上人工确认的空白页；识别为空的页先计为待处理，失败页不计入覆盖。
          <br />
          <b>页数未知</b>时显示“统计中”，不会用 0 或假百分比代替。
          <br />
          <b>重复导入</b>按内容哈希去重：相同文件会关联到已有文献记录，不复制成第二本。
        </p>
      </div>

      <ImportDialog
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onQueued={() => {
          bumpDataVersion();
          refreshCounts();
        }}
      />
      <BibDialog
        doc={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          bumpDataVersion();
          documents.reload();
        }}
      />
    </div>
  );
}