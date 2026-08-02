import { useEffect, useRef, useState } from "react";

import { DairyBenchApi } from "./api";
import { CompanyTable } from "./components/CompanyTable";
import { EmptyState } from "./components/EmptyState";
import { MetricChart } from "./components/MetricChart";
import { OperationsTimeline } from "./components/OperationsTimeline";
import { RunForm, type RunControl } from "./components/RunForm";
import { SummaryCards } from "./components/SummaryCards";
import { TokenSummary } from "./components/TokenSummary";
import { isAbortError, requestErrorMessage } from "./requestErrors";
import { isActiveRun } from "./runStatus";
import type {
  PolicyMode,
  PolicyProfileView,
  RunJobView,
  RunRequest,
} from "./types";
import { useRunWorkspace } from "./useRunWorkspace";

const api = new DairyBenchApi();
const MAX_SEED = 2_147_483_647;

interface AppError {
  readonly heading: string;
  readonly message: string;
}

interface RunNotice {
  readonly message: string;
  readonly runId: string;
}

type RunTransition = Extract<
  RunControl,
  { readonly state: "starting" | "stopping" }
>;

export function App() {
  const workspace = useRunWorkspace(api);
  const [mode, setMode] = useState<PolicyMode>("baseline");
  const [profiles, setProfiles] = useState<readonly PolicyProfileView[]>([]);
  const [seed, setSeed] = useState("42");
  const [progress, setProgress] = useState<RunJobView | null>(null);
  const [runTransition, setRunTransition] = useState<RunTransition | null>(null);
  const [error, setError] = useState<AppError | null>(null);
  const [notice, setNotice] = useState<RunNotice | null>(null);
  const activeRequest = useRef<AbortController | null>(null);
  const replaySourceActivated = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    void api
      .policyProfiles(controller.signal)
      .then(setProfiles)
      .catch((reason: unknown) => {
        if (!isAbortError(reason)) {
          setError({
            heading: "Policy profiles could not be loaded",
            message: requestErrorMessage(reason, {
              fallback: "The backend policy profiles could not be loaded.",
            }),
          });
        }
      });

    return () => {
      controller.abort();
      activeRequest.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (mode !== "replay") {
      replaySourceActivated.current = false;
      return;
    }
    if (
      replaySourceActivated.current ||
      !workspace.selectedReplaySourceId
    ) {
      return;
    }
    replaySourceActivated.current = true;
    if (workspace.selectedRunId !== workspace.selectedReplaySourceId) {
      workspace.selectReplaySource(workspace.selectedReplaySourceId);
    }
  }, [
    mode,
    workspace.selectReplaySource,
    workspace.selectedReplaySourceId,
    workspace.selectedRunId,
  ]);

  async function runBenchmark(): Promise<void> {
    const request = buildRunRequest(
      mode,
      seed,
      workspace.selectedReplaySourceId,
    );
    if (typeof request === "string") {
      setError({ heading: "The run could not be started", message: request });
      return;
    }

    const controller = new AbortController();
    activeRequest.current?.abort();
    activeRequest.current = controller;
    setError(null);
    setNotice(null);
    setProgress(null);
    setRunTransition({ state: "starting" });
    let selectedSubmittedRun = false;
    let streamedRunId = "";

    try {
      const result = await api.run(
        request,
        (job) => {
          streamedRunId = job.runId;
          setProgress(job);
          setRunTransition((current) =>
            current?.state === "starting" ? null : current,
          );
          const selectLatestDay =
            !selectedSubmittedRun ||
            !isActiveRun(job.status);
          workspace.trackJob(job, selectLatestDay);
          selectedSubmittedRun = true;
        },
        controller.signal,
      );
      if (result.status === "stopped") {
        setNotice({ message: stopNotice(result), runId: result.runId });
      }
    } catch (reason: unknown) {
      if (isAbortError(reason)) {
        return;
      }
      setProgress(null);
      setError({
        heading: "The run could not be completed",
        message: requestErrorMessage(reason, {
          fallback:
            "The run stopped before completion. Its persisted timeline remains available below.",
          network:
            "Could not reach the backend. Confirm FastAPI is running at 127.0.0.1:8000.",
        }),
      });
    } finally {
      setRunTransition((current) =>
        current?.state === "starting" ? null : current,
      );
      workspace.releaseJobStream(streamedRunId);
      if (activeRequest.current === controller) {
        activeRequest.current = null;
      }
    }
  }

  async function stopBenchmark(runId: string): Promise<void> {
    setError(null);
    setNotice(null);
    setRunTransition({ state: "stopping", runId });
    try {
      const job = await api.stopRun(runId);
      setProgress(job);
      workspace.trackJob(job, true);
      if (job.status === "stopped") {
        setNotice({ message: stopNotice(job), runId: job.runId });
      } else if (job.status === "completed") {
        setNotice({
          message: "The run completed before the stop request took effect.",
          runId: job.runId,
        });
      }
    } catch (reason: unknown) {
      setError({
        heading: "The run could not be stopped",
        message: requestErrorMessage(reason, {
          fallback:
            "The stop request failed. The run is still running and will continue to be monitored.",
          network:
            "Could not reach the backend. The run is still running and will continue to be monitored.",
        }),
      });
    } finally {
      setRunTransition(null);
    }
  }

  const { selectedDay, selectedJob } = workspace;
  const replaySourceError =
    mode === "replay" ? workspace.replaySourceError : null;
  const displayedError = error?.message ?? workspace.error ?? replaySourceError;
  const errorHeading =
    error?.heading ??
    (workspace.error
      ? "Run data could not be loaded"
      : "Replay sources could not be loaded");
  const episode =
    workspace.episode?.runId === selectedJob?.runId ? workspace.episode : null;
  const activeProgress =
    progress && isActiveRun(progress.status) ? progress : workspace.activeJob;
  const runControl: RunControl =
    runTransition ??
    (activeProgress
      ? { state: "running", runId: activeProgress.runId }
      : { state: "idle" });
  const displayedNotice =
    (notice !== null && notice.runId === selectedJob?.runId
      ? notice.message
      : null) ??
    (selectedJob?.status === "stopped" && selectedJob.startedAt === null
      ? stopNotice(selectedJob)
      : null);
  const timelineDay =
    selectedJob?.status === "stopped" && selectedJob.startedAt === null
      ? null
      : selectedDay;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span aria-hidden="true" className="brand-mark">
            D
          </span>
          <span>
            <strong>Dairy Bench</strong>
            <small>Independent company agents · perishable dairy economy</small>
          </span>
        </div>
        <RunForm
          control={runControl}
          mode={mode}
          onModeChange={setMode}
          onRun={() => void runBenchmark()}
          onSeedChange={setSeed}
          onSourceRunIdChange={workspace.selectReplaySource}
          onStop={(runId) => void stopBenchmark(runId)}
          profiles={profiles}
          replaySources={workspace.replaySources}
          replaySourcesLoading={workspace.isReplaySourceLoading}
          seed={seed}
          sourceRunId={workspace.selectedReplaySourceId}
        />
      </header>

      <main>
        <section className="hero">
          <div>
            <span className="eyebrow">
              {(selectedJob?.scenarioId ?? "flow.dairy.base.s9.v3").toUpperCase()}
            </span>
            <h1>Independent companies. One living dairy economy.</h1>
            <p>
              Each company uses an independent agent to make event-driven,
              atomic decisions across production, two spot markets,
              processing, retail, and perishable inventory settlement.
            </p>
          </div>
          {selectedJob && (
            <dl className="run-meta">
              <div>
                <dt>Run</dt>
                <dd>{selectedJob.runId}</dd>
              </div>
              <div>
                <dt>Seed</dt>
                <dd>{selectedJob.seed}</dd>
              </div>
              <div>
                <dt>Horizon</dt>
                <dd>{selectedJob.totalDays} days</dd>
              </div>
            </dl>
          )}
        </section>

        <div aria-live="polite">
          {displayedError && (
            <div className="error-banner" role="alert">
              <span aria-hidden="true">!</span>
              <div>
                <strong>{errorHeading}</strong>
                <p>{displayedError}</p>
              </div>
              <button
                onClick={() => {
                  setError(null);
                  workspace.clearError();
                }}
                type="button"
              >
                Dismiss
              </button>
            </div>
          )}
          {displayedNotice && (
            <div className="notice-banner" role="status">
              <span>{displayedNotice}</span>
            </div>
          )}
          {runControl.state !== "idle" && (
            <div className="loading-banner" role="status">
              <span className="spinner dark" />
              <div className="progress-copy">
                <span>{progressText(runControl, activeProgress)}</span>
                <progress
                  max={activeProgress?.totalDays ?? 30}
                  value={activeProgress?.currentDay ?? 0}
                />
              </div>
            </div>
          )}
        </div>

        {!selectedJob ? (
          <EmptyState />
        ) : (
          <div className="dashboard">
            {workspace.isEpisodeLoading && (
              <div className="loading-banner result-loading" role="status">
                <span className="spinner dark" />
                Loading the completed score and company results…
              </div>
            )}
            {workspace.episodeError && (
              <div className="error-banner result-error" role="alert">
                <span aria-hidden="true">!</span>
                <div>
                  <strong>The final score is unavailable</strong>
                  <p>{workspace.episodeError}</p>
                </div>
              </div>
            )}
            {episode && (
              <>
                <SummaryCards score={episode.score} />
                {episode.agentUsage && (
                  <TokenSummary summary={episode.agentUsage} />
                )}
                <CompanyTable companies={episode.companies} />
                <MetricChart snapshots={episode.snapshots} />
              </>
            )}
            {timelineDay !== null && (
              <OperationsTimeline
                day={timelineDay}
                days={selectedJob.totalDays}
                key={selectedJob.runId}
                loadDetail={loadTimelineDetail}
                loadTimeline={loadTimelineDay}
                onDayChange={workspace.selectDay}
                revision={`${selectedJob.status}:${selectedJob.currentDay}:${workspace.timelineRevision}`}
                runId={selectedJob.runId}
                timelinePending={selectedJob.status === "queued"}
              />
            )}
          </div>
        )}
      </main>

      <footer>
        <span>Dairy Bench V3</span>
        <span>API keys and Codex credentials remain on the backend.</span>
      </footer>
    </div>
  );
}

function buildRunRequest(
  mode: PolicyMode,
  seed: string,
  sourceRunId: string,
): RunRequest | string {
  if (mode === "replay") {
    return sourceRunId
      ? { policyMode: mode, sourceRunId }
      : "Exact replay requires a completed source run ID.";
  }

  const parsedSeed = Number(seed);
  return Number.isInteger(parsedSeed) &&
    parsedSeed >= 0 &&
    parsedSeed <= MAX_SEED
    ? { policyMode: mode, seed: parsedSeed }
    : `The random seed must be an integer from 0 to ${MAX_SEED}.`;
}

function progressText(
  control: RunControl,
  progress: RunJobView | null,
): string {
  if (control.state === "starting") {
    return "Starting run…";
  }
  if (control.state === "stopping") {
    return "Stopping run…";
  }
  if (!progress || progress.status === "queued") {
    return "Run queued. Preparing independent company agents…";
  }
  if (progress.status === "interrupted") {
    return "Resuming an interrupted run…";
  }
  return `Running day ${progress.currentDay} of ${progress.totalDays}`;
}

function stopNotice(job: RunJobView): string {
  return job.startedAt === null
    ? "Run stopped before execution began. No timeline was produced."
    : "Run stopped. Its recorded timeline remains available below.";
}

function loadTimelineDay(
  runId: string,
  day: number,
  signal?: AbortSignal,
) {
  return api.timelineDay(runId, day, signal);
}

function loadTimelineDetail(
  runId: string,
  entryId: string,
  signal?: AbortSignal,
) {
  return api.timelineDetail(runId, entryId, signal);
}
