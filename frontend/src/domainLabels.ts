const PRODUCT_LABELS: Readonly<Record<string, string>> = {
  bottled_milk: "Bottled milk",
  raw_milk: "Raw milk",
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
  const payload = JSON.stringify(data, null, 2);
  return payload ?? "null";
}

export function humanizeIdentifier(value: string): string {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
