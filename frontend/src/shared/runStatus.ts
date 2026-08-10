import type { RunStatus } from "./api/types";

export function isActiveRun(status: RunStatus): boolean {
  return status === "queued" || status === "running";
}

export function isResumableRun(status: RunStatus): boolean {
  return status === "interrupted" || status === "stopped";
}
