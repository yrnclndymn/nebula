// Pure client-side filter predicates (#152) — kept out of the component modules so
// the filter-bar controls and the pages that own the filter state can share them.

// Headcount range predicate (#7). Companies with no headcount are excluded whenever
// a bound is set — an unknown size can't satisfy ">200". Empty bounds and
// unparseable input are ignored, so a blank range keeps every row.
export function headcountInRange(
  headcount: number | null | undefined,
  min: string,
  max: string,
): boolean {
  const lo = min.trim() === "" ? null : Number(min);
  const hi = max.trim() === "" ? null : Number(max);
  const hasLo = lo !== null && !Number.isNaN(lo);
  const hasHi = hi !== null && !Number.isNaN(hi);
  if (!hasLo && !hasHi) return true;
  if (headcount == null) return false;
  if (hasLo && headcount < lo) return false;
  if (hasHi && headcount > hi) return false;
  return true;
}
