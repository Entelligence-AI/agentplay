import { useEffect, useMemo, useState } from 'react';
import { sessionWindowRange } from '@/core/sessionWindow';
import type { WindowKey } from '@/core/windowOptions';

/** A cache-busting key that changes at EITHER the viewer's local midnight
 * or UTC midnight, whichever comes first (not a real ISO date). Two
 * different fields of `sessionWindowRange`'s result roll over on two
 * different clocks: `daysThisMonth` derives MTD's `from` from local
 * `getDate()`, but `to` (every window) is `toISOString().slice(0, 10)`,
 * a UTC date. A key on only one clock stays stale across the other
 * boundary for up to the viewer's UTC offset. */
const dateBoundaryKey = (d: Date) =>
  `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}-${d.toISOString().slice(0, 10)}`;

/** The list's date range for `windowKey`, kept fresh across midnight. */
export function useWindowRange(windowKey: WindowKey) {
  // `sessionWindowRange` is keyed off `new Date()` at call time, but a memo
  // keyed only on `windowKey` never re-runs that call once the tab has been
  // open a while - the range (mtd's start, and every window's `to`) goes
  // stale after a real calendar day rolls over while the page stays mounted
  //. A cheap poll refreshes it within a minute of that,
  // keyed by both clocks it can roll over on (see `dateBoundaryKey`).
  const [today, setToday] = useState(() => dateBoundaryKey(new Date()));
  useEffect(() => {
    const id = setInterval(() => {
      const next = dateBoundaryKey(new Date());
      setToday((prev) => (prev === next ? prev : next));
    }, 60_000);
    return () => clearInterval(id);
  }, []);
  return useMemo(
    // `today` is not read in the body; it exists purely to invalidate this
    // memo once the poll above notices the calendar date has moved on.
    () => sessionWindowRange(windowKey, new Date()),
    [windowKey, today]
  );
}
