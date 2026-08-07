import type { RunStatus } from "./api/types";

export function isActiveRun(status: RunStatus): boolean {
  return status === "queued" || status === "running" || status === "interrupted";
}

export function isGracefulRunTerminal(status: RunStatus): boolean {
  return status === "completed" || status === "stopped";
}
