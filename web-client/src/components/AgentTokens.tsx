import { useEffect, useState } from 'react';
import { apiDelete, apiGet, apiPost, peekCsrfToken } from '../api/client';

interface Token { id: string; name: string; scope: string; expires_at: number; revoked_at: number | null; }
export function AgentTokens() {
  const [items, setItems] = useState<Token[]>([]); const [name, setName] = useState('Codex');
  const [scope, setScope] = useState('read'); const [days, setDays] = useState(90);
  const [secret, setSecret] = useState(''); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const load = () => apiGet<{ items: Token[] }>('/agent-tokens').then((data) => setItems(data.items));
  useEffect(() => { void load().catch((cause: Error) => setError(cause.message)); }, []);
  const act = async (work: () => Promise<void>) => { setBusy(true); setError('');
    try { await work(); await load(); } catch (cause) { setError((cause as Error).message); } finally { setBusy(false); } };
  return <section className="set-block"><h2 className="set-h">Codex 连接</h2>
    <p className="set-sub">每台设备使用单独的令牌，可随时撤销。编辑权限允许创建待核摘录及编辑笔记。</p>
    <form className="btn-row" onSubmit={(event) => { event.preventDefault(); void act(async () => {
      const result = await apiPost<{ token: string }>('/agent-tokens', { name, scope, days }); setSecret(result.token);
    }); }}><label>名称<input aria-label="令牌名称" value={name} maxLength={100} required onChange={(e) => setName(e.target.value)} /></label>
      <label>权限<select aria-label="令牌权限" value={scope} onChange={(e) => setScope(e.target.value)}><option value="read">只读</option><option value="edit">编辑笔记与待核摘录</option></select></label>
      <label>有效天数<input aria-label="令牌有效天数" type="number" min={1} max={365} value={days} onChange={(e) => setDays(Number(e.target.value))} /></label>
      <button className="btn btn-primary" disabled={busy || !!secret}>创建令牌</button></form>
    {error ? <p role="alert">{error}</p> : null}
    {secret ? <div role="status"><p>明文仅在此显示一次，请存入你自己的凭据管理器。</p><input aria-label="新令牌（仅显示一次）" readOnly type="password" value={secret} autoComplete="off" />
      <button type="button" className="btn btn-sm" onClick={() => void navigator.clipboard.writeText(secret)}>复制</button>
      <button type="button" className="btn btn-sm" onClick={() => setSecret('')}>已保存，关闭明文</button></div> : null}
    <p>在运行 Codex 的环境中设置 <code>LITWORKBENCH_TOKEN</code>，然后连接：</p>
    <pre style={{ overflowX: 'auto' }}>{`codex mcp add litworkbench --url ${location.origin}/mcp --bearer-token-env-var LITWORKBENCH_TOKEN`}</pre>
    <ul className="plan-list">{items.map((item) => <li key={item.id}>{item.name} · {item.scope === 'edit' ? '编辑' : '只读'} · 到期 {new Date(item.expires_at * 1000).toLocaleDateString()}
      {item.revoked_at ? ' · 已撤销' : <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void act(async () => { await apiDelete(`/agent-tokens/${item.id}`); })}>撤销</button>}</li>)}</ul>
    <button type="button" className="btn btn-sm" onClick={() => {
      void fetch('/auth/logout', { method: 'POST', headers: { 'X-CSRF-Token': peekCsrfToken() || '' } }).then((response) => {
        if (response.ok) location.assign('/pages'); else setError('退出失败，请重新连接后重试。');
      }).catch(() => setError('无法连接服务器。'));
    }}>退出网页登录</button>
  </section>;
}
