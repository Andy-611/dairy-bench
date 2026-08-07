import type { RunJobView } from "../../shared/api/types";
import { isResumableRun } from "../../shared/runStatus";

interface AllRunsProps {
  readonly busy: boolean;
  readonly canLoadMore: boolean;
  readonly jobs: readonly RunJobView[];
  readonly loading: boolean;
  readonly onLoadMore: () => void;
  readonly onResume: (runId: string) => void;
  readonly onSelect: (runId: string) => void;
  readonly resumingRunId: string | null;
  readonly selectedJob: RunJobView | null;
}

export function AllRuns({
  busy,
  canLoadMore,
  jobs,
  loading,
  onLoadMore,
  onResume,
  onSelect,
  resumingRunId,
  selectedJob,
}: AllRunsProps) {
  const resumable = selectedJob && isResumableRun(selectedJob.status);

  return (
    <>
      <label className="run-field all-runs-field">
        <span>Run history</span>
        <span className="sr-only">Selected run</span>
        <select
          disabled={loading && jobs.length === 0}
          onChange={(event) => onSelect(event.target.value)}
          value={selectedJob?.runId ?? ""}
        >
          {jobs.length === 0 ? (
            <option value="">{loading ? "Loading runs..." : "No runs recorded"}</option>
          ) : (
            jobs.map((job) => (
              <option key={job.runId} value={job.runId}>
                {runLabel(job)}
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
          {resumingRunId === selectedJob.runId
            ? "Resuming..."
            : selectedJob.status === "failed"
              ? "Retry from checkpoint"
              : "Resume run"}
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

function runLabel(job: RunJobView): string {
  const policy =
    job.mode === "model"
      ? (job.model ?? "NewAPI model")
      : job.mode === "replay"
        ? "completed replay"
        : "rule baseline";
  return `${job.status.toUpperCase()} · ${shortRunId(job.runId)} · ${policy} · day ${job.currentDay}/${job.totalDays}`;
}

function shortRunId(runId: string): string {
  return runId.length > 20 ? `${runId.slice(0, 20)}...` : runId;
}
