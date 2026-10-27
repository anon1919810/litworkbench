import { expect, it } from 'vitest';
import { pageTree } from './tree';

it('keeps each subtree together and orders siblings by position', () => {
  const rows = [
    { id: 'child', parent_id: 'a', position: 0 }, { id: 'b', parent_id: null, position: 1 },
    { id: 'a', parent_id: null, position: 0 }, { id: 'orphan', parent_id: 'filtered', position: 2 },
  ];
  expect(pageTree(rows).map(({ item, depth }) => [item.id, depth])).toEqual([['a', 0], ['child', 1], ['b', 0], ['orphan', 0]]);
});
