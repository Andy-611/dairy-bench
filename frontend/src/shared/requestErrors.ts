interface ErrorMessageOptions {
  readonly fallback: string;
  readonly network?: string;
}

export function isAbortError(reason: unknown): boolean {
  return reason instanceof DOMException && reason.name === "AbortError";
}

export function requestErrorMessage(
  reason: unknown,
  { fallback, network }: ErrorMessageOptions,
): string {
  if (reason instanceof TypeError && network !== undefined) {
    return network;
  }
  return reason instanceof Error ? reason.message : fallback;
}
