import { IconSettings } from '../components/Icons';
import { PageHeader } from '../components/PageHeader';
import { Tag } from '../components/Tag';
import { summarizeCoverage } from '../lib/coverage';
import { num } from '../lib/format';
import { useAppState } from '../state/AppState';
import { AgentTokens } from '../components/AgentTokens';

/**
 * 设置页只显示本地服务的真实只读信息，并对尚未实现的能力给出诚实说明。
 * 不接收任何密钥：后端还没有对应接口，放一个输入框只会让人误以为已经保存。
 */
export function SettingsPage() {
  const { workspace, coverage, refreshCounts } = useAppState();
  const summary = summarizeCoverage(coverage);

  return (
    <div className="page">
      <PageHeader
        icon={<IconSettings size={30} />}
        title="设置"
        description="本页显示本地服务的真实状态。模型与并发设置属于后续版本，当前不会接收任何密钥或改动配置。"
      />
      <div className="page-body">
        {workspace?.mode === 'cloud' ? <AgentTokens /> : null}
        <div className="set-block">
          <h2 className="set-h">当前工作区</h2>
          <p className="set-sub">这些值直接来自本机服务的会话接口，刷新页面会重新读取。</p>
          <dl className="kv-l" style={{ marginTop: 10 }}>
            <dt>工作区名称</dt>
            <dd>{workspace?.name || '—'}</dd>
            <dt>数据目录</dt>
            <dd style={{ overflowWrap: 'anywhere' }}>{workspace?.root || '—'}</dd>
            <dt>OCR 引擎</dt>
            <dd>
              {workspace?.ocr?.available ? <Tag tone="info">可用</Tag> : <Tag tone="error">不可用</Tag>}
              {workspace?.ocr?.detail ? <span style={{ marginLeft: 8 }}>{workspace.ocr.detail}</span> : null}
            </dd>
            <dt>覆盖</dt>
            <dd>{summary.text}</dd>
          </dl>
          <div className="btn-row" style={{ marginTop: 12 }}>
            <button type="button" className="btn btn-sm" onClick={refreshCounts}>
              刷新覆盖统计
            </button>
          </div>
        </div>

        <div className="set-block">
          <h2 className="set-h">后续版本才会提供的设置</h2>
          <p className="set-sub">下列项目在本机服务里还没有对应接口，因此这里既不显示输入框，也不接受任何内容。</p>
          <div className="set-row">
            <div className="set-label">
              模型服务地址与密钥
              <div className="set-desc">将来按“只在本次服务运行的内存中使用”设计，界面只回显“已配置”。</div>
            </div>
            <div className="set-ctl">
              <Tag tone="neutral">未实现</Tag>
              <span className="help">本版本不接收、不保存、不发送任何密钥，也不会写入浏览器存储。</span>
            </div>
          </div>
          <div className="set-row">
            <div className="set-label">
              OCR 并发数
              <div className="set-desc">默认单个 OCR 工作进程；可调范围与前台阅读的隔离策略需要实测后确定。</div>
            </div>
            <div className="set-ctl">
              <Tag tone="neutral">未实现</Tag>
            </div>
          </div>
          <div className="set-row">
            <div className="set-label">
              单文件大小上限
              <div className="set-desc">本机服务当前固定为 1 GiB；页面不接受修改。</div>
            </div>
            <div className="set-ctl">
              <Tag tone="neutral">固定 1 GiB</Tag>
            </div>
          </div>
          <div className="set-row">
            <div className="set-label">
              备份位置
              <div className="set-desc">备份与迁移由数据目录本身决定；导出功能属于第二里程碑。</div>
            </div>
            <div className="set-ctl">
              <Tag tone="neutral">未实现</Tag>
            </div>
          </div>
        </div>

        <div className="set-block">
          <h2 className="set-h">接口与计数口径</h2>
          <ul className="plan-list">
            <li>
              分页默认 30 条，页大小最大 100
              <span className="plan-sub">列表翻页都带上一页码，越界由前端夹到有效范围。</span>
            </li>
            <li>
              覆盖 = 已索引文字页 + 人工确认空白页
              <span className="plan-sub">
                当前：{summary.ratio ? `${num(summary.ratio.covered)}/${num(summary.ratio.known)} 页` : '页数统计中'}
                {coverage?.pending_pdf_pages ? `，待处理 ${num(coverage.pending_pdf_pages)} 页` : ''}
                {coverage?.failed_pdf_pages ? `，失败 ${num(coverage.failed_pdf_pages)} 页` : ''}
              </span>
            </li>
            <li>
              会话与令牌
              <span className="plan-sub">页面加载时建立 HttpOnly 会话 cookie；csrf 令牌只保存在内存，服务重启后自动重新握手。</span>
            </li>
            <li>
              本版本不做永久删除
              <span className="plan-sub">摘录只能改为“已拒绝”，历史与校订记录都会保留。</span>
            </li>
          </ul>
        </div>

        <p className="fine-print">本页所有数值均来自本机服务的实时响应，界面不缓存任何演示数据。</p>
      </div>
    </div>
  );
}
