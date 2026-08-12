import { useEffect, useRef, useState } from "react";

import { CompanyTable } from "../features/evaluation/CompanyTable";
import { EvaluationStandard } from "../features/evaluation/EvaluationStandard";
import { MetricChart } from "../features/evaluation/MetricChart";
import { RunQualityBanner } from "../features/evaluation/RunQualityBanner";
import { TokenSummary } from "../features/evaluation/TokenSummary";
import {
  RunForm,
  type RunControl,
  type RunFormMode,
} from "../features/runs/RunForm";
import { OperationsTimeline } from "../features/timeline/OperationsTimeline";
import { DairyBenchApi } from "../shared/api/client";
import type {
  PolicyMode,
  PolicyProfileView,
  RunJobView,
  RunRequest,
} from "../shared/api/types";
import { isAbortError, requestErrorMessage } from "../shared/requestErrors";
import { completedWeeks, DAYS_PER_WEEK } from "../shared/simulationCalendar";
import { EmptyState } from "../shared/ui/EmptyState";
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
  { readonly state: "resuming" | "starting" | "stopping" }
>;

type RunSubmission = (signal: AbortSignal) => Promise<RunJobView>;

export function App() {
  const workspace = useRunWorkspace(api);
  const [mode, setMode] = useState<RunFormMode>("baseline");
  const [profiles, setProfiles] = useState<readonly PolicyProfileView[]>([]);
  const [model, setModel] = useState("");
  const [seed, setSeed] = useState("42");
  const [runTransition, setRunTransition] = useState<RunTransition | null>(null);
  const [error, setError] = useState<AppError | null>(null);
  const [notice, setNotice] = useState<RunNotice | null>(null);
  const activeRequest = useRef<AbortController | null>(null);
  const replaySourceActivated = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    void api
      .policyProfiles(controller.signal)
      .then((loadedProfiles) => {
        setProfiles(loadedProfiles);
        const modelProfile = loadedProfiles.find(
          (profile) => profile.mode === "model",
        );
        setModel(
          (current) =>
            current || modelProfile?.model || modelProfile?.models[0] || "",
        );
      })
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

  async function submitJob(
    transition: RunTransition,
    errorHeading: string,
    operation: RunSubmission,
  ): Promise<void> {
    const controller = new AbortController();
    activeRequest.current = controller;
    setError(null);
    setNotice(null);
    setRunTransition(transition);

    try {
      const job = await operation(controller.signal);
      workspace.trackJob(job, true);
    } catch (reason: unknown) {
      if (isAbortError(reason)) {
        return;
      }
      setError({
        heading: errorHeading,
        message: requestErrorMessage(reason, {
          fallback: "The backend did not accept the run request.",
          network:
            "Could not reach the backend. Confirm FastAPI is running at 127.0.0.1:8000.",
        }),
      });
    } finally {
      setRunTransition((current) => (current === transition ? null : current));
      if (activeRequest.current === controller) {
        activeRequest.current = null;
      }
    }
  }

  async function runBenchmark(): Promise<void> {
    if (mode === "all-runs") {
      return;
    }
    const request = buildRunRequest(
      mode,
      model,
      seed,
      workspace.selectedReplaySourceId,
    );
    if (typeof request === "string") {
      setError({ heading: "The run could not be started", message: request });
      return;
    }
    await submitJob(
      { state: "starting" },
      "The run could not be started",
      (signal) => api.submitRun(request, signal),
    );
  }

  async function resumeBenchmark(runId: string): Promise<void> {
    await submitJob(
      { state: "resuming", runId },
      "The run could not be resumed",
      (signal) => api.resumeRun(runId, signal),
    );
  }

  async function stopBenchmark(runId: string): Promise<void> {
    setError(null);
    setNotice(null);
    setRunTransition({ state: "stopping", runId });
    try {
      const job = await api.stopRun(runId);
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

  const { selectedJob, selectedWeek } = workspace;
  const replaySourceError =
    mode === "replay" ? workspace.replaySourceError : null;
  const selectedFailure =
    selectedJob?.status === "failed" ? selectedJob.errorMessage : null;
  const displayedError =
    error?.message ?? workspace.error ?? replaySourceError ?? selectedFailure;
  const errorCanBeDismissed = Boolean(error || workspace.error || replaySourceError);
  const errorHeading =
    error?.heading ??
    (workspace.error
      ? "Run data could not be loaded"
      : replaySourceError
        ? "Completed replay sources could not be loaded"
        : "The selected run failed");
  const episode =
    workspace.episode?.runId === selectedJob?.runId ? workspace.episode : null;
  const activeProgress =
    workspace.activeJobs.find((job) => job.runId === selectedJob?.runId) ??
    workspace.activeJobs[0] ??
    null;
  const runControl: RunControl = runTransition ?? { state: "idle" };
  const displayedNotice =
    (notice !== null && notice.runId === selectedJob?.runId
      ? notice.message
      : null) ??
    (selectedJob?.status === "stopped"
      ? stopNotice(selectedJob)
      : selectedJob?.status === "interrupted"
        ? interruptionNotice(selectedJob)
        : null);
  const timelineWeek =
    selectedJob?.status === "stopped" && selectedJob.startedAt === null
      ? null
      : selectedWeek;

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
          canLoadMoreRuns={workspace.canLoadMoreRuns}
          jobs={workspace.jobs}
          model={model}
          control={runControl}
          mode={mode}
          onModeChange={setMode}
          onLoadMoreRuns={workspace.loadMoreRuns}
          onModelChange={setModel}
          onRun={() => void runBenchmark()}
          onSeedChange={setSeed}
          onSourceRunIdChange={workspace.selectReplaySource}
          onResume={(runId) => void resumeBenchmark(runId)}
          onRunSelect={workspace.selectRun}
          onStop={(runId) => void stopBenchmark(runId)}
          profiles={profiles}
          replaySources={workspace.replaySources}
          replaySourcesLoading={workspace.isReplaySourceLoading}
          runHistoryLoading={workspace.isRunHistoryLoading}
          seed={seed}
          selectedJob={selectedJob}
          sourceRunId={workspace.selectedReplaySourceId}
        />
      </header>

      <main>
        <section className="hero">
          <div>
            <span className="eyebrow">
              {(selectedJob?.scenarioId ?? "flow.dairy.base.s9.v6").toUpperCase()}
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
                <dd>{selectedJob.totalWeeks} weeks</dd>
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
              {errorCanBeDismissed && (
                <button
                  onClick={() => {
                    setError(null);
                    workspace.clearError();
                  }}
                  type="button"
                >
                  Dismiss
                </button>
              )}
            </div>
          )}
          {displayedNotice && (
            <div className="notice-banner" role="status">
              <span>{displayedNotice}</span>
            </div>
          )}
          {runTransition && (
            <div className="loading-banner" role="status">
              <span className="spinner dark" />
              <div className="progress-copy">
                <span>{transitionText(runTransition)}</span>
              </div>
            </div>
          )}
          {activeProgress && (
            <div className="loading-banner" role="status">
              <span className="spinner dark" />
              <div className="progress-copy">
                <span>
                  {activeRunText(
                    workspace.activeJobs.length,
                    activeProgress,
                    activeProgress.runId === selectedJob?.runId,
                  )}
                </span>
                <progress
                  max={activeProgress.totalWeeks * DAYS_PER_WEEK}
                  value={activeProgress.currentAbsoluteDay}
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
                <RunQualityBanner quality={episode.quality} />
                <EvaluationStandard
                  benchmarkEligible={episode.quality.benchmarkEligible}
                  score={episode.score}
                />
                {episode.agentUsage && (
                  <TokenSummary summary={episode.agentUsage} />
                )}
                <CompanyTable companies={episode.companies} />
                <MetricChart snapshots={episode.snapshots} />
              </>
            )}
            {timelineWeek !== null && (
              <OperationsTimeline
                key={selectedJob.runId}
                loadDetail={loadTimelineDetail}
                loadTimeline={loadTimelineWeek}
                onWeekChange={workspace.selectWeek}
                revision={`${selectedJob.status}:${selectedJob.currentAbsoluteDay}:${workspace.timelineRevision}`}
                runId={selectedJob.runId}
                timelinePending={selectedJob.status === "queued"}
                week={timelineWeek}
                weeks={selectedJob.totalWeeks}
              />
            )}
          </div>
        )}
      </main>

      <footer>
        <span>Dairy Bench V6</span>
        <span>NewAPI credentials remain on the backend.</span>
      </footer>
    </div>
  );
}

function buildRunRequest(
  mode: PolicyMode,
  model: string,
  seed: string,
  sourceRunId: string,
): RunRequest | string {
  if (mode === "replay") {
    return sourceRunId
      ? { policyMode: mode, sourceRunId }
      : "Completed Run Replay requires a completed source run ID.";
  }

  const parsedSeed = Number(seed);
  if (!Number.isInteger(parsedSeed) || parsedSeed < 0 || parsedSeed > MAX_SEED) {
    return `The random seed must be an integer from 0 to ${MAX_SEED}.`;
  }
  if (mode === "model") {
    return model
      ? { policyMode: mode, model, seed: parsedSeed }
      : "Model mode requires a configured NewAPI model.";
  }
  return { policyMode: mode, seed: parsedSeed };
}

function transitionText(control: RunTransition): string {
  if (control.state === "starting") {
    return "Starting run…";
  }
  if (control.state === "resuming") {
    return "Restoring the saved checkpoint…";
  }
  if (control.state === "stopping") {
    return "Stopping run…";
  }
  return "Updating run…";
}

function activeRunText(
  count: number,
  job: RunJobView,
  selected: boolean,
): string {
  const summary = `${count} active ${count === 1 ? "run" : "runs"}`;
  const subject = selected ? "Selected run" : "Latest active run";
  return job.status === "queued"
    ? `${summary}. ${subject} is queued.`
    : `${summary}. ${subject} has completed ${completedWeeks(job.currentAbsoluteDay)} of ${job.totalWeeks} weeks (${job.currentAbsoluteDay} simulated days).`;
}

function stopNotice(job: RunJobView): string {
  if (job.stopReason === "quota_exhausted") {
    return "NewAPI balance was exhausted. Recorded progress and any checkpoint were preserved; recharge NewAPI, then resume this run.";
  }
  return job.startedAt === null
    ? "Run stopped before execution began. It can be resumed from the beginning."
    : "Run stopped. Its timeline and checkpoint remain available for resume.";
}

function interruptionNotice(job: RunJobView): string {
  const reason = job.errorMessage ? ` ${job.errorMessage}` : "";
  return `Run interrupted by a temporary infrastructure issue.${reason} Its timeline and checkpoint remain available for resume.`;
}

function loadTimelineWeek(
  runId: string,
  week: number,
  signal?: AbortSignal,
) {
  return api.timelineWeek(runId, week, signal);
}

function loadTimelineDetail(
  runId: string,
  entryId: string,
  signal?: AbortSignal,
) {
  return api.timelineDetail(runId, entryId, signal);
}
