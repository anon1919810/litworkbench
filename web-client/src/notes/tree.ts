interface Node { id: string; parent_id: string | null; position: number; }
export function pageTree<T extends Node>(items: T[]): { item: T; depth: number }[] {
  const ids = new Set(items.map((item) => item.id));
  const groups = new Map<string | null, T[]>();
  for (const item of items) {
    const parent = item.parent_id && ids.has(item.parent_id) ? item.parent_id : null;
    groups.set(parent, [...(groups.get(parent) || []), item]);
  }
  for (const values of groups.values()) values.sort((a, b) => a.position - b.position || a.id.localeCompare(b.id));
  const result: { item: T; depth: number }[] = []; const visited = new Set<string>();
  const visit = (parent: string | null, depth: number) => {
    for (const item of groups.get(parent) || []) {
      if (visited.has(item.id)) continue;
      visited.add(item.id); result.push({ item, depth }); visit(item.id, depth + 1);
    }
  };
  visit(null, 0);
  return result;
}
