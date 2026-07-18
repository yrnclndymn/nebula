// Persistence for the table's column order (drag-to-reorder survives reloads).
// Separate from CompanyTable.tsx so that file only exports components (fast refresh).

const ORDER_KEY = "nebula.columnOrder";

export function loadColumnOrder(): string[] {
  try {
    const raw = localStorage.getItem(ORDER_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

export function saveColumnOrder(ids: string[]): void {
  try {
    localStorage.setItem(ORDER_KEY, JSON.stringify(ids));
  } catch {
    /* localStorage unavailable — order just won't persist */
  }
}
