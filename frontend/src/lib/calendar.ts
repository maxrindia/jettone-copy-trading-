/**
 * Date-only calendar helpers for the DatePicker. Everything works on
 * "YYYY-MM-DD" strings (the same value the expiry field stores and the API
 * receives) and never converts through a local-time Date, so there is no
 * timezone drift.
 */

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

export interface YearMonth {
  year: number;
  /** 0 = January */
  month: number;
}

const pad = (n: number, width = 2) => String(n).padStart(width, "0");

export function toIso(year: number, month: number, day: number): string {
  return `${pad(year, 4)}-${pad(month + 1)}-${pad(day)}`;
}

/** Parses a real calendar date; returns null for anything else (e.g. "2026-02-30"). */
export function parseIso(iso: string | null | undefined): { year: number; month: number; day: number } | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso ?? "");
  if (!m) return null;
  const [year, month, day] = [Number(m[1]), Number(m[2]) - 1, Number(m[3])];
  const d = new Date(Date.UTC(year, month, day));
  if (d.getUTCFullYear() !== year || d.getUTCMonth() !== month || d.getUTCDate() !== day) return null;
  return { year, month, day };
}

export function daysInMonth({ year, month }: YearMonth): number {
  return new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
}

export function shiftMonth({ year, month }: YearMonth, delta: number): YearMonth {
  const index = year * 12 + month + delta;
  return { year: Math.floor(index / 12), month: ((index % 12) + 12) % 12 };
}

export function monthLabel({ year, month }: YearMonth): string {
  return `${MONTH_NAMES[month]} ${year}`;
}

/** Weeks (Sunday first) of ISO dates; days outside the month are null. */
export function monthGrid(ym: YearMonth): (string | null)[][] {
  const firstWeekday = new Date(Date.UTC(ym.year, ym.month, 1)).getUTCDay();
  const cells: (string | null)[] = Array(firstWeekday).fill(null);
  for (let day = 1; day <= daysInMonth(ym); day++) cells.push(toIso(ym.year, ym.month, day));
  while (cells.length % 7 !== 0) cells.push(null);
  const weeks: (string | null)[][] = [];
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));
  return weeks;
}

/** Today's date as YYYY-MM-DD on the given IANA calendar (default: the trading timezone, IST). */
export function todayIso(timeZone = "Asia/Kolkata"): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
}
