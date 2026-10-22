/**
 * 集合（专题）管理对话框：新建与改名。
 *
 * 没有删除按钮：摘录集不提供永久删除语义，集合也一样。
 * 需要「下线」一个集合时改名为「（归档）」或把其中的摘录移出。
 */
import { useEffect, useState } from 'react';
import { createCollection, patchCollection } from '../api/endpoints';
import type { Collection } from '../api/types';
import { Modal } from '../components/Modal';
import { describeError } from '../lib/conflict';
import { num } from '../lib/format';

export function CollectionsDialog({
  open,
  collections,
  onClose,
  onChanged,
}: {
  open: boolean;
  collections: Collection[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setName('');
      setDescription('');
      setError(null);
    }
  }, [open]);

  if (!open) return null;

  const submit = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await createCollection({ name, description });
      setName('');
      setDescription('');
      onChanged();
    } catch (caught) {
      setError(describeError(caught).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      title="摘录集合"
      note="集合用于把摘录归入专题（例如「文物建筑资料」）。同一摘录可以属于多个集合；集合不能删除，只能改名或把摘录移出。"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            关闭
          </button>
          <button type="button" className="btn btn-primary" disabled={busy} onClick={submit}>
            {busy ? '创建中…' : '新建集合'}
          </button>
        </>
      }
    >
      {error ? (
        <div className="conflict-box" role="alert">
          {error}
        </div>
      ) : null}

      <div className="field">
        <label className="lbl" htmlFor="collection-name">
          名称（1–80 字，全库唯一）
        </label>
        <input
          id="collection-name"
          type="text"
          value={name}
          maxLength={80}
          onChange={(event) => setName(event.target.value)}
          style={{ width: '100%' }}
          placeholder="例如：文物建筑资料"
        />
      </div>
      <div className="field">
        <label className="lbl" htmlFor="collection-desc">
          说明（最多 1000 字）
        </label>
        <textarea
          id="collection-desc"
          rows={3}
          maxLength={1000}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
      </div>

      <div className="field">
        <span className="lbl">已有集合（{num(collections.length)}）</span>
        {collections.length === 0 ? (
          <p className="help">还没有集合。先建一个，再在摘录集里勾选并批量加入。</p>
        ) : (
          <ul className="plan-list">
            {collections.map((collection) => (
              <CollectionRow key={collection.id} collection={collection} onChanged={onChanged} />
            ))}
          </ul>
        )}
      </div>
    </Modal>
  );
}

function CollectionRow({ collection, onChanged }: { collection: Collection; onChanged: () => void }) {
  const [name, setName] = useState(collection.name);
  const [description, setDescription] = useState(collection.description);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setName(collection.name);
    setDescription(collection.description);
  }, [collection.id, collection.name, collection.description]);

  const save = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await patchCollection(collection.id, { name, description });
      onChanged();
    } catch (caught) {
      setError(describeError(caught).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <li>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <input
          type="text"
          aria-label={`集合名称：${collection.name}`}
          value={name}
          maxLength={80}
          onChange={(event) => setName(event.target.value)}
          style={{ flex: '1 1 180px' }}
        />
        <span className="num">{num(collection.evidence_count)} 条</span>
        <button type="button" className="btn btn-sm" disabled={busy} onClick={save}>
          {busy ? '保存中…' : '保存'}
        </button>
      </div>
      <input
        type="text"
        aria-label={`集合说明：${collection.name}`}
        placeholder="说明（可留空）"
        value={description}
        maxLength={1000}
        onChange={(event) => setDescription(event.target.value)}
        style={{ width: '100%', marginTop: 6 }}
      />
      {error ? <p className="error-text">{error}</p> : null}
    </li>
  );
}