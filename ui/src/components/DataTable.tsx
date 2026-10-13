/** Компактная стеклянная таблица с состояниями загрузки/пустоты. */
import type { ReactNode } from 'react';

export interface Column<T> {
  key: string;
  title: ReactNode;
  render: (row: T, index: number) => ReactNode;
  className?: string;
}

interface Props<T> {
  columns: Column<T>[];
  rows: T[];
  loading?: boolean;
  empty?: ReactNode;
  rowKey: (row: T, index: number) => string | number;
  maxHeight?: number;
  onRowClick?: (row: T) => void;
}

export function DataTable<T>({ columns, rows, loading, empty, rowKey, maxHeight, onRowClick }: Props<T>) {
  if (loading && rows.length === 0) {
    return (
      <div className="space-y-2">
        {[0, 1, 2, 3].map((index) => (
          <div key={index} className="skeleton h-10 w-full" />
        ))}
      </div>
    );
  }
  if (rows.length === 0) {
    return <div className="py-10 text-center text-sm text-[var(--text-3)]">{empty ?? 'Нет данных'}</div>;
  }
  return (
    <div className="overflow-auto" style={maxHeight ? { maxHeight } : undefined}>
      <table className="table-glass">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.key} className={column.className}>
                {column.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr
              key={rowKey(row, index)}
              className={onRowClick ? 'cursor-pointer' : undefined}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
            >
              {columns.map((column) => (
                <td key={column.key} className={column.className}>
                  {column.render(row, index)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
