const PRODUCT_LABELS: Readonly<Record<string, string>> = {
  bottled_milk: "Bottled milk",
  raw_milk: "Raw milk",
};

const AUDIT_VALUE_LABELS: Readonly<Record<string, string>> = {
  "\u76d2\u88c5\u5976": "Bottled milk",
  "\u539f\u5976": "Raw milk",
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

export function formatAuditPayload(data: unknown): string {
  const payload = JSON.stringify(
    data,
    (_key, value: unknown) =>
      typeof value === "string" ? (AUDIT_VALUE_LABELS[value] ?? value) : value,
    2,
  );
  return payload ?? "null";
}

export function humanizeIdentifier(value: string): string {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
