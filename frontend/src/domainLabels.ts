const COMPANY_LABELS: Readonly<Record<string, string>> = {
  farm_a: "Farm A",
  farm_b: "Farm B",
  processor_a: "Processor A",
  processor_b: "Processor B",
  retailer_a: "Retailer A",
  retailer_b: "Retailer B",
};

const PRODUCT_LABELS: Readonly<Record<string, string>> = {
  bottled_milk: "Bottled milk",
  raw_milk: "Raw milk",
};

const LEGACY_DOMAIN_LABELS: Readonly<Record<string, string>> = {
  "\u52a0\u5de5\u5382 A": "Processor A",
  "\u52a0\u5de5\u5382 B": "Processor B",
  "\u7267\u573a A": "Farm A",
  "\u7267\u573a B": "Farm B",
  "\u76d2\u88c5\u5976": "Bottled milk",
  "\u539f\u5976": "Raw milk",
  "\u96f6\u552e\u5546 A": "Retailer A",
  "\u96f6\u552e\u5546 B": "Retailer B",
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
  return (
    COMPANY_LABELS[companyId] ??
    humanizeIdentifier(companyId)
  );
}

export function productLabel(productId: string): string {
  return PRODUCT_LABELS[productId] ?? humanizeIdentifier(productId);
}

export function formatAuditPayload(data: unknown): string {
  const payload = JSON.stringify(
    data,
    (_key, value: unknown) =>
      typeof value === "string" ? (LEGACY_DOMAIN_LABELS[value] ?? value) : value,
    2,
  );
  return payload ?? "null";
}

export function humanizeIdentifier(value: string): string {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
