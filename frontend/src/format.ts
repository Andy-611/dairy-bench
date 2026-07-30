import type { CompanyRole } from "./types";

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

export function formatSignedValue(value: number): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${formatValue(value)}`;
}

export function formatPercent(value: number): string {
  return percentFormatter.format(value);
}

export function formatGrowth(value: number): string {
  return `${value.toFixed(3)}x`;
}
