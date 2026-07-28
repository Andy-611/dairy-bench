import type { CompanyRole, JsonValue } from "./types";

const valueFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 2,
  minimumFractionDigits: 0,
});

const percentFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 1,
  minimumFractionDigits: 1,
  style: "percent",
});

export const ROLE_LABELS: Readonly<Record<CompanyRole, string>> = {
  farm: "牧场",
  processor: "加工厂",
  retailer: "零售商",
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
  return `${value.toFixed(3)}×`;
}

export function formatEventValue(value: JsonValue): string {
  if (typeof value === "number") {
    return formatValue(value);
  }
  if (typeof value === "boolean") {
    return value ? "是" : "否";
  }
  if (value === null) {
    return "—";
  }
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value);
}
