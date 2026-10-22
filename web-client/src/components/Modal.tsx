import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';

/**
 * 基于原生 <dialog> 的模态框：Esc 关闭、点遮罩关闭、打开时把焦点移入对话框。
 */
interface ModalProps {
  open: boolean;
  title: string;
  note?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}

export function Modal({ open, title, note, onClose, children, footer, wide }: ModalProps) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    if (open && !node.open) node.showModal();
    if (!open && node.open) node.close();
  }, [open]);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const onCancel = (event: Event) => {
      event.preventDefault();
      onClose();
    };
    const onClick = (event: MouseEvent) => {
      if (event.target === node) onClose();
    };
    node.addEventListener('cancel', onCancel);
    node.addEventListener('click', onClick);
    node.addEventListener('close', onClose);
    return () => {
      node.removeEventListener('cancel', onCancel);
      node.removeEventListener('click', onClick);
      node.removeEventListener('close', onClose);
    };
  }, [onClose]);

  return (
    <dialog className={wide ? 'modal wide' : 'modal'} ref={ref} aria-label={title}>
      <form method="dialog" className="modal-head">
        <div style={{ minWidth: 0, flex: '1 1 auto' }}>
          <h2>{title}</h2>
          {note ? <p className="modal-note">{note}</p> : null}
        </div>
        <button className="icon-btn" value="close" aria-label="关闭对话框" type="submit">
          ✕
        </button>
      </form>
      <div className="modal-body">{children}</div>
      {footer ? <div className="modal-foot">{footer}</div> : null}
    </dialog>
  );
}
