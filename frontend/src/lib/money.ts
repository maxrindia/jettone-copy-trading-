/**
 * The one money formatter for P&L across the app.
 *
 * Input is the engine's 2-decimal money string ("-1.52"), or null when the
 * value could not be verified. The engine never sends floats for money, so
 * the sign and digits are read straight from the string - no float
 * arithmetic ever touches an amount.
 */
const MINUS = "−";
const MONEY_PATTERN = /^(-)?(\d+)(?:\.(\d+))?$/;

export type MoneyTone = "positive" | "negative" | "neutral";

interface ParsedMoney {
  negative: boolean;
  zero: boolean;
  integer: string;
  fraction: string;
}

function parseMoney(value: string | null | undefined): ParsedMoney | null {
  if (value === null || value === undefined) return null;
  const match = MONEY_PATTERN.exec(value.trim());
  if (!match) return null;
  const rawFraction = match[3] ?? "";
  const integer = match[2].replace(/^0+(?=\d)/, "");
  const zero = /^0+$/.test(integer) && /^0*$/.test(rawFraction);
  return {
    negative: match[1] === "-" && !zero,
    zero,
    integer,
    fraction: rawFraction.padEnd(2, "0").slice(0, 2),
  };
}

/** Indian digit grouping: 1,23,45,678. */
function groupIndian(integer: string): string {
  if (integer.length <= 3) return integer;
  const head = integer.slice(0, -3);
  const tail = integer.slice(-3);
  return `${head.replace(/\B(?=(\d{2})+(?!\d))/g, ",")},${tail}`;
}

/** "+₹1.52", "−₹1.52" (true minus sign), or "₹0.00" (including when there is no verified value). */
export function formatInr(value: string | null | undefined): string {
  const money = parseMoney(value);
  if (!money) return "₹0.00";
  const amount = `₹${groupIndian(money.integer)}.${money.fraction}`;
  if (money.zero) return amount;
  return `${money.negative ? MINUS : "+"}${amount}`;
}

export function moneyTone(value: string | null | undefined): MoneyTone {
  const money = parseMoney(value);
  if (!money || money.zero) return "neutral";
  return money.negative ? "negative" : "positive";
}

const TONE_CLASS: Record<MoneyTone, string> = {
  positive: "text-green-600",
  negative: "text-red-600",
  neutral: "text-[#6B7280]",
};

export function moneyToneClass(value: string | null | undefined): string {
  return TONE_CLASS[moneyTone(value)];
}
