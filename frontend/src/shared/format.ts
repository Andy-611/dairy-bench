import type { CompanyRole, DecimalText } from "./api/types";

const valueFormatter = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 2,
  minimumFractionDigits: 0,
});

const percentFormatter = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 1,
  minimumFractionDigits: 1,
  style: "percent",
});

export const ROLE_LABELS: Readonly<Record<CompanyRole, string>> = {
  farm: "Farm",
  processor: "Processor",
  retailer: "Retailer",
};

export function formatValue(value: number): string {
  return valueFormatter.format(value);
}

export function formatExactDecimal(value: DecimalText): string {
  return value;
}

export function formatMarketPrice(value: DecimalText): string {
  return value;
}

export function isZeroDecimal(value: string): boolean {
  const mantissa = value.split(/[eE]/, 1)[0] ?? value;
  return !/[1-9]/.test(mantissa);
}

export function formatSignedExactDecimal(value: DecimalText): string {
  const unsigned = /^[+-]/.test(value) ? value.slice(1) : value;
  if (
    value.startsWith("-") ||
    value.startsWith("+") ||
    isZeroDecimal(unsigned)
  ) {
    return value;
  }
  return `+${value}`;
}

export function formatPercent(value: number): string {
  return percentFormatter.format(value);
}

export function formatGrowth(value: DecimalText): string {
  return `${value}x`;
}
