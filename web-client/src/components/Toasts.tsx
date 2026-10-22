import { useAppState } from '../state/AppState';

export function Toasts() {
  const { toasts, dismissToast } = useAppState();
  if (!toasts.length) return null;
  return (
    <div className="toast-wrap" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <div key={toast.id} className={toast.tone === 'error' ? 'toast toast-error' : toast.tone === 'warn' ? 'toast toast-warn' : 'toast'}>
          <span>{toast.text}</span>
          <button type="button" className="t-close" onClick={() => dismissToast(toast.id)} aria-label="关闭提示">
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}