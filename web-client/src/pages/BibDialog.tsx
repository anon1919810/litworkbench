import { useEffect, useState } from 'react';
import type { DocumentItem } from '../api/types';
import { Modal } from '../components/Modal';
import { describeError } from '../lib/conflict';
import { patchDocument } from '../api/endpoints';

interface BibDialogProps {
  doc: DocumentItem | null;
  onClose: () => void;
  onSaved: () => void;
}

/** 书目字段编辑：只允许契约允许的字段，不触碰原件与已提取文字。 */
export function BibDialog({ doc, onClose, onSaved }: BibDialogProps) {
  const [form, setForm] = useState({ title: '', author: '', year: '', edition: '', volume: '' });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!doc) return;
    setForm({
      title: doc.title ?? '',
      author: doc.author ?? '',
      year: doc.year ?? '',
      edition: doc.edition ?? '',
      volume: doc.volume ?? '',
    });
    setError(null);
  }, [doc]);

  if (!doc) return null;

  const fields: Array<{ key: keyof typeof form; label: string }> = [
    { key: 'title', label: '书名' },
    { key: 'author', label: '编者 / 作者' },
    { key: 'year', label: '出版年' },
    { key: 'edition', label: '版次' },
    { key: 'volume', label: '卷次' },
  ];

  return (
    <Modal
      open
      title="编辑书目"
      note={`文件名：${doc.filename}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={saving}
            onClick={async () => {
              setSaving(true);
              setError(null);
              try {
                await patchDocument(doc.id, {
                  title: form.title.trim() || null,
                  author: form.author.trim() || null,
                  year: form.year.trim() || null,
                  edition: form.edition.trim() || null,
                  volume: form.volume.trim() || null,
                });
                onSaved();
                onClose();
              } catch (caught) {
                setError(describeError(caught).message);
              } finally {
                setSaving(false);
              }
            }}
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      {error ? (
        <div className="conflict-box" role="alert">
          {error}
        </div>
      ) : null}
      {fields.map((field) => (
        <div className="field" key={field.key}>
          <label className="lbl" htmlFor={`bib-${field.key}`}>
            {field.label}
          </label>
          <input
            id={`bib-${field.key}`}
            type="text"
            value={form[field.key]}
            onChange={(event) => setForm((current) => ({ ...current, [field.key]: event.target.value }))}
            style={{ width: '100%' }}
          />
        </div>
      ))}
      <p className="fine-print" style={{ marginTop: 0 }}>
        书目信息只用于显示与检索过滤；修改不会改变原件、页序或已保存的摘录内容。
      </p>
    </Modal>
  );
}