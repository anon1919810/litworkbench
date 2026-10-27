import { useEffect, useState } from 'react';
import { reauthenticate } from '../api/client';

export function LoginLink() {
  return <a className="btn btn-primary" href={`/auth/login?return_to=${encodeURIComponent(location.pathname + location.search)}`}
    target="_blank" rel="noopener noreferrer">在新标签页登录</a>;
}

/** Login in a separate tab keeps all pending editor state in the original tab. */
export function CloudLoginNotice() {
  const [visible, setVisible] = useState(false); const [error, setError] = useState('');
  useEffect(() => { const show = () => setVisible(true); window.addEventListener('lit-login-required', show);
    return () => window.removeEventListener('lit-login-required', show); }, []);
  if (!visible) return null;
  return <aside role="alert" style={{ position: 'fixed', bottom: 20, right: 20, zIndex: 500, maxWidth: 460,
    background: '#fff', border: '1px solid #e2d5bb', padding: 20, boxShadow: '0 4px 30px #0002' }}>
    <strong>登录已失效，草稿仍保留在当前标签页</strong><p>在新标签页登录后，回来重新连接，再重试保存。</p>
    <div className="btn-row"><LoginLink /><button type="button" className="btn" onClick={() => {
      void reauthenticate().then(() => { setVisible(false); setError(''); }).catch((cause: Error) => setError(cause.message));
    }}>已登录，重新连接</button></div>{error ? <p>{error}</p> : null}
  </aside>;
}
