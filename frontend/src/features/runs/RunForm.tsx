import type { FormEvent } from "react";

import type {
  PolicyMode,
  PolicyProfileView,
  ReplaySourceView,
  RunJobView,
} from "../../shared/api/types";
import { AllRuns } from "./AllRuns";

export type RunFormMode = PolicyMode | "all-runs";

export type RunControl =
  | { readonly state: "idle" }
  | { readonly state: "starting" }
  | { readonly state: "resuming"; readonly runId: string }
  | { readonly state: "stopping"; readonly runId: string };

interface RunFormProps {
  readonly canLoadMoreRuns: boolean;
  readonly jobs: readonly RunJobView[];
  readonly model: string;
  readonly mode: RunFormMode;
  readonly profiles: readonly PolicyProfileView[];
  readonly replaySources: readonly ReplaySourceView[];
  readonly replaySourcesLoading: boolean;
  readonly seed: string;
  readonly selectedJob: RunJobView | null;
  readonly sourceRunId: string;
  readonly control: RunControl;
  readonly onLoadMoreRuns: () => void;
  readonly onModeChange: (mode: RunFormMode) => void;
  readonly onModelChange: (model: string) => void;
  readonly onSeedChange: (seed: string) => void;
  readonly onSourceRunIdChange: (runId: string) => void;
  readonly onRun: () => void;
  readonly onRunSelect: (runId: string) => void;
  readonly onResume: (runId: string) => void;
  readonly onStop: (runId: string) => void;
  readonly runHistoryLoading: boolean;
}

export function RunForm({
  canLoadMoreRuns,
  jobs,
  model,
  mode,
  profiles,
  replaySources,
  replaySourcesLoading,
  seed,
  selectedJob,
  sourceRunId,
  control,
  onLoadMoreRuns,
  onModelChange,
  onModeChange,
  onSeedChange,
  onSourceRunIdChange,
  onRun,
  onRunSelect,
  onResume,
  onStop,
  runHistoryLoading,
}: RunFormProps) {
  const selectedProfile = profiles.find((profile) => profile.mode === mode);
  const formLocked = control.state !== "idle";
  const canStart =
    Boolean(selectedProfile?.available) &&
    (mode !== "replay" || Boolean(sourceRunId)) &&
    (mode !== "model" || Boolean(model));

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (control.state === "idle" && canStart) {
      onRun();
    }
  }

  return (
    <form className="run-form" onSubmit={handleSubmit}>
      <label className="run-field mode-field">
        <span>Company policy</span>
        <select
          disabled={formLocked}
          onChange={(event) => onModeChange(event.target.value as RunFormMode)}
          value={mode}
        >
          {profiles.map((profile) => (
            <option
              disabled={!profile.available}
              key={profile.mode}
              value={profile.mode}
            >
              {profile.label}
              {!profile.available ? " (not configured)" : ""}
            </option>
          ))}
          <option value="all-runs">All Runs</option>
        </select>
      </label>

      {mode === "all-runs" ? (
        <AllRuns
          busy={formLocked}
          canLoadMore={canLoadMoreRuns}
          jobs={jobs}
          loading={runHistoryLoading}
          onLoadMore={onLoadMoreRuns}
          onResume={onResume}
          onSelect={onRunSelect}
          onStop={onStop}
          resumingRunId={control.state === "resuming" ? control.runId : null}
          selectedJob={selectedJob}
          stoppingRunId={control.state === "stopping" ? control.runId : null}
        />
      ) : mode === "replay" ? (
        <label className="run-field replay-field">
          <span>Source run ID</span>
          <select
            disabled={
              formLocked || replaySourcesLoading || replaySources.length === 0
            }
            name="sourceRunId"
            onChange={(event) => onSourceRunIdChange(event.target.value)}
            required
            value={sourceRunId}
          >
            {replaySourcesLoading ? (
              <option value="">Loading completed runs…</option>
            ) : replaySources.length === 0 ? (
              <option value="">No completed runs available</option>
            ) : (
              replaySources.map((source) => (
                <option key={source.runId} value={source.runId}>
                  {replaySourceLabel(source)}
                </option>
              ))
            )}
          </select>
        </label>
      ) : (
        <label className="run-field seed-field">
          <span>Random seed</span>
          <input
            aria-describedby="seed-help"
            autoComplete="off"
            disabled={formLocked}
            inputMode="numeric"
            max={2_147_483_647}
            min={0}
            name="seed"
            onChange={(event) => onSeedChange(event.target.value)}
            required
            step={1}
            type="number"
            value={seed}
          />
        </label>
      )}

      {mode === "model" && selectedProfile && (
        <label className="run-field model-field">
          <span>Model</span>
          <select
            disabled={formLocked || selectedProfile.models.length === 0}
            name="model"
            onChange={(event) => onModelChange(event.target.value)}
            required
            value={model}
          >
            {selectedProfile.models.map((model) => (
              <option key={model} value={model}>
                {model}
              </option>
            ))}
          </select>
        </label>
      )}

      <span className="sr-only" id="seed-help">
        Use the same seed to reproduce the economic environment.
      </span>
      {mode !== "all-runs" && (
        <RunActionButton
          canStart={canStart}
          completedReplay={mode === "replay"}
          control={control}
        />
      )}
      {mode === "all-runs" && (
        <span className="profile-hint">
          Browse every persisted run, including incomplete runs.
        </span>
      )}
      {selectedProfile && (
        <span className="profile-hint" title={selectedProfile.description}>
          {mode === "replay"
            ? "Creates a new deterministic run from a completed source; no agents are called."
            : mode === "model" && model
            ? [selectedProfile.provider, model].filter(Boolean).join(" / ")
            : selectedProfile.description}
          {!selectedProfile.available && selectedProfile.unavailableReason
            ? `: ${selectedProfile.unavailableReason}`
            : ""}
        </span>
      )}
    </form>
  );
}

interface RunActionButtonProps {
  readonly canStart: boolean;
  readonly completedReplay: boolean;
  readonly control: RunControl;
}

function RunActionButton({
  canStart,
  completedReplay,
  control,
}: RunActionButtonProps) {
  if (
    control.state === "starting" ||
    control.state === "resuming" ||
    control.state === "stopping"
  ) {
    return (
      <button className="run-button" disabled type="button">
        <span aria-hidden="true" className="spinner" />
        {control.state === "starting"
          ? "Starting…"
          : control.state === "resuming"
            ? "Resuming…"
            : "Stopping…"}
      </button>
    );
  }

  return (
    <button className="run-button" disabled={!canStart} type="submit">
      <span aria-hidden="true">▶</span>
      {completedReplay ? "Start completed replay" : "Run 30 days"}
    </button>
  );
}

function replaySourceLabel(source: ReplaySourceView): string {
  const quality = source.benchmarkEligible ? "benchmark eligible" : "diagnostic only";
  return `${shortRunId(source.runId)} · ${quality} · ${formatDateTime(source.submittedAt)}`;
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
