import { act, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it } from 'vitest';
import { useApiQuery, type QueryState } from '../hooks/useApiQuery';
import { bulkAvailability, createSelection, emptyScope, toggleOne } from '../lib/evidenceSelection';
import { ExportDialog } from '../pages/ExportDialog';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it('keeps old rows disabled when the new filter request fails', async () => {
  const container = document.createElement('div');
  const root = createRoot(container);
  let current!: QueryState<{ id: string }>;
  let rejectNew!: (error: Error) => void;
  function Probe({ queryKey }: { queryKey: string }) {
    current = useApiQuery(queryKey, () => queryKey === 'A'
      ? Promise.resolve({ id: 'row-from-A' })
      : new Promise((_, reject) => { rejectNew = reject; }));
    return null;
  }
  try {
    await act(async () => root.render(<Probe queryKey="A" />));
    expect(current.data?.id).toBe('row-from-A');
    await act(async () => root.render(<Probe queryKey="B" />));
    expect(current.stale).toBe(true);
    await act(async () => rejectNew(new Error('new filter failed')));
    expect(current.loading).toBe(false);
    expect(current.stale).toBe(true);
    const selection = toggleOne(createSelection('B'), current.data!.id);
    expect(bulkAvailability(selection, current.stale, current.loading).enabled).toBe(false);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});

it('reopens the export dialog after native dialog close', async () => {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  function Harness() {
    const [show, setShow] = useState(false);
    return <>
      <button id="open-export" onClick={() => setShow(true)}>open export</button>
      {show ? <ExportDialog scope={emptyScope()} selectedIds={[]} listTotal={1}
        listStale={false} onClose={() => setShow(false)} onNotice={() => {}} /> : null}
    </>;
  }
  try {
    await act(async () => root.render(<Harness />));
    await act(async () => container.querySelector<HTMLButtonElement>('#open-export')!.click());
    const dialog = container.querySelector<HTMLDialogElement>('dialog')!;
    expect(dialog.open).toBe(true);
    // method=dialog closes the element without dispatching a cancel event.
    await act(async () => dialog.close());
    expect(dialog.open).toBe(false);
    await act(async () => container.querySelector<HTMLButtonElement>('#open-export')!.click());
    expect(container.querySelector<HTMLDialogElement>('dialog')?.open).toBe(true);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
