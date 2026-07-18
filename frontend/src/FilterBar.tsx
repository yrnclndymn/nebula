import type { ReactNode } from "react";

// Shared filter-row plumbing (#152) — the companies table, the What's-new feed,
// the digest history and the M&A page each rebuilt the same `.filters` /
// `whatsnew-filters` row inline, with the same "All X" select + Clear button
// boilerplate. `FilterBar` is the flex row; the small controls below dedupe the
// repeated markup while leaving each page free to mix in its own bespoke inputs
// (a search box, the digest picker) as children.

// The filter row container. `variant="whatsnew"` adds the news-page padding class.
export function FilterBar({
  variant,
  children,
}: {
  variant?: "whatsnew";
  children: ReactNode;
}) {
  return (
    <div className={variant === "whatsnew" ? "filters whatsnew-filters" : "filters"}>
      {children}
    </div>
  );
}

export type FilterOption = { value: string; label: string };

// A dropdown that always leads with an "all" option (empty value) followed by the
// caller's options. Selecting the first option clears the filter.
export function FilterSelect({
  value,
  onChange,
  allLabel,
  options,
}: {
  value: string;
  onChange: (value: string) => void;
  allLabel: string;
  options: FilterOption[];
}) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{allLabel}</option>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

// The "Clear" reset button. Callers render it only when a filter is active.
export function ClearFiltersButton({ onClear }: { onClear: () => void }) {
  return (
    <button className="clear" onClick={onClear}>
      Clear
    </button>
  );
}

// Headcount min/max range (#7). Values are strings ("" = unbounded); the page owns
// the state and the client-side filtering — see `headcountInRange` in `./filters`.
export function HeadcountRange({
  min,
  max,
  onMin,
  onMax,
}: {
  min: string;
  max: string;
  onMin: (value: string) => void;
  onMax: (value: string) => void;
}) {
  return (
    <span className="headcount-range">
      <input
        type="number"
        min={0}
        inputMode="numeric"
        placeholder="min ⌀"
        aria-label="Minimum headcount"
        value={min}
        onChange={(e) => onMin(e.target.value)}
      />
      <input
        type="number"
        min={0}
        inputMode="numeric"
        placeholder="max ⌀"
        aria-label="Maximum headcount"
        value={max}
        onChange={(e) => onMax(e.target.value)}
      />
    </span>
  );
}
