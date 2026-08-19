import type { PolicyProfileView, RunJobView } from "../../shared/api/types";
import { shortRunId } from "../../shared/labels";
import { isActiveRun, isResumableRun } from "../../shared/runStatus";
import { completedWeeks } from "../../shared/simulationCalendar";

interface AllRunsProps {
  readonly busy: boolean;
  readonly canLoadMore: boolean;
  readonly jobs: readonly RunJobView[];
  readonly loading: boolean;
  readonly onLoadMore: () => void;
  readonly onResume: (runId: string) => void;
  readonly onSelect: (runId: string) => void;
  readonly onStop: (runId: string) => void;
  readonly profiles: readonly PolicyProfileView[];
  readonly resumingRunId: string | null;
  readonly selectedJob: RunJobView | null;
  readonly stoppingRunId: string | null;
}

export function AllRuns({
  busy,
  canLoadMore,
  jobs,
  loading,
  onLoadMore,
  onResume,
  onSelect,
  onStop,
  profiles,
  resumingRunId,
  selectedJob,
  stoppingRunId,
}: AllRunsProps) {
  const resumable = selectedJob && isResumableRun(selectedJob.status);
  const active = selectedJob && isActiveRun(selectedJob.status);

  return (
    <>
      <label className="run-field all-runs-field">
        <span>Run history</span>
        <span className="sr-only">Selected run</span>
        <select
          disabled={busy || (loading && jobs.length === 0)}
          onChange={(event) => onSelect(event.target.value)}
          value={selectedJob?.runId ?? ""}
        >
          {jobs.length === 0 ? (
            <option value="">{loading ? "Loading runs..." : "No runs recorded"}</option>
          ) : (
            jobs.map((job) => (
              <option key={job.runId} value={job.runId}>
                {runLabel(job, profiles)}
              </option>
            ))
          )}
        </select>
      </label>
      {resumable && (
        <button
          className="history-action resume-run-button"
          disabled={busy}
          onClick={() => onResume(selectedJob.runId)}
          type="button"
        >
          {resumingRunId === selectedJob.runId ? "Resuming..." : "Resume run"}
        </button>
      )}
      {active && (
        <button
          className="history-action stop-history-button"
          disabled={busy}
          onClick={() => confirmStop(selectedJob.runId, onStop)}
          type="button"
        >
          {stoppingRunId === selectedJob.runId ? "Stopping..." : "Stop run"}
        </button>
      )}
      {canLoadMore && (
        <button
          className="history-action"
          disabled={loading}
          onClick={onLoadMore}
          type="button"
        >
          {loading ? "Loading..." : "Load more"}
        </button>
      )}
    </>
  );
}

function confirmStop(runId: string, onStop: (runId: string) => void): void {
  if (
    window.confirm(
      "Stop this run? Recorded activity and its checkpoint will remain available for resume.",
    )
  ) {
    onStop(runId);
  }
}

function runLabel(
  job: RunJobView,
  profiles: readonly PolicyProfileView[],
): string {
  const profile =
    profiles.find((candidate) => candidate.profileId === job.profileId)?.label ??
    job.profileId;
  const policy = job.mode === "model" && job.model ? `${profile} / ${job.model}` : profile;
  const status = runStatusLabel(job);
  return `${status} · ${shortRunId(job.runId)} · ${policy} · week ${completedWeeks(job.currentAbsoluteDay)}/${job.totalWeeks}`;
}

function runStatusLabel(job: RunJobView): string {
  if (job.status === "completed" && job.quality?.benchmarkEligible === false) {
    return "COMPLETED · DIAGNOSTIC";
  }
  if (job.status === "stopped" && job.stopReason === "quota_exhausted") {
    return "STOPPED · QUOTA EXHAUSTED";
  }
  return job.status.toUpperCase();
}
