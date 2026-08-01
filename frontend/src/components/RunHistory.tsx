import type { RunJobView, RunStatus } from "../types";

interface RunHistoryProps {
  readonly error: string | null;
  readonly isLoading: boolean;
  readonly jobs: readonly RunJobView[];
  readonly onRefresh: () => void;
  readonly onSelect: (runId: string) => void;
  readonly selectedJob: RunJobView | null;
  readonly selectedRunId: string;
}

const STATUS_LABELS: Readonly<Record<RunStatus, string>> = {
  completed: "Completed",
  failed: "Failed",
  interrupted: "Interrupted",
  queued: "Queued",
  running: "Running",
};

export function RunHistory({
  error,
  isLoading,
  jobs,
  onRefresh,
  onSelect,
  selectedJob,
  selectedRunId,
}: RunHistoryProps) {
  return (
    <section aria-labelledby="run-history-title" className="panel run-history">
      <div className="run-history-picker">
        <div>
          <span className="eyebrow">RUN HISTORY</span>
          <h2 id="run-history-title">Open a persisted run</h2>
          <p>Viewing a run never creates a replay or calls an agent.</p>
        </div>
        <label>
          <span className="sr-only">Historical run</span>
          <select
            disabled={jobs.length === 0}
            onChange={(event) => onSelect(event.target.value)}
            value={selectedRunId}
          >
            {!selectedRunId && <option value="">Select a run</option>}
            {selectedRunId && !selectedJob && (
              <option value={selectedRunId}>Selected · {shortRunId(selectedRunId)}</option>
            )}
            {jobs.map((job) => (
              <option key={job.runId} value={job.runId}>
                {historyOption(job)}
              </option>
            ))}
          </select>
        </label>
        <button disabled={isLoading} onClick={onRefresh} type="button">
          {isLoading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {error && <p className="run-history-error">{error}</p>}
      {selectedJob && <RunJobStatus job={selectedJob} />}
    </section>
  );
}

function RunJobStatus({ job }: { readonly job: RunJobView }) {
  const progress = Math.min(job.currentDay, job.totalDays);
  return (
    <div className="run-job-status">
      <span className={`run-status ${job.status}`}>
        {STATUS_LABELS[job.status]}
      </span>
      <dl>
        <div>
          <dt>Run ID</dt>
          <dd title={job.runId}>{job.runId}</dd>
        </div>
        <div>
          <dt>Policy</dt>
          <dd>{job.mode === "replay" ? "Exact replay" : job.mode}</dd>
        </div>
        <div>
          <dt>Progress</dt>
          <dd>{progress} of {job.totalDays} days complete</dd>
        </div>
        <div>
          <dt>Submitted</dt>
          <dd>{formatDateTime(job.submittedAt)}</dd>
        </div>
      </dl>
      {job.status !== "completed" && (
        <progress max={job.totalDays} value={progress} />
      )}
      {job.errorMessage && (
        <p className="run-job-error" role="alert">
          <strong>Run error</strong>
          {job.errorMessage}
        </p>
      )}
    </div>
  );
}

function historyOption(job: RunJobView): string {
  return `${STATUS_LABELS[job.status]} · ${shortRunId(job.runId)} · ${formatDateTime(job.submittedAt)}`;
}

function shortRunId(runId: string): string {
  return runId.length > 18 ? `${runId.slice(0, 18)}…` : runId;
}

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
