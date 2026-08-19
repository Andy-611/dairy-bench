import type { CompanyRole } from "./api/types";

const PRODUCT_LABELS: Readonly<Record<string, string>> = {
  bottled_milk: "Bottled milk",
  raw_milk: "Raw milk",
};

export const ROLE_LABELS: Readonly<Record<CompanyRole, string>> = {
  farm: "Farm",
  processor: "Processor",
  retailer: "Retailer",
};

export function companyLabel(
  companyId: string | null,
  backendName?: string,
): string {
  if (companyId === null) {
    return "System";
  }
  const normalizedName = backendName?.trim();
  if (normalizedName && /^[\x20-\x7E]+$/.test(normalizedName)) {
    return normalizedName;
  }
  return humanizeIdentifier(companyId);
}

export function productLabel(productId: string): string {
  return PRODUCT_LABELS[productId] ?? humanizeIdentifier(productId);
}

export function humanizeIdentifier(value: string): string {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function shortRunId(runId: string): string {
  return runId.length > 20 ? `${runId.slice(0, 20)}…` : runId;
}
