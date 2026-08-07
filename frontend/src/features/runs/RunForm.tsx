import type { FormEvent } from "react";

import type {
  PolicyMode,
  PolicyProfileView,
  ReplaySourceView,
} from "../../shared/api/types";

export type RunControl =
  | { readonly state: "idle" }
  | { readonly state: "starting" }
  | { readonly state: "running"; readonly runId: string }
  | { readonly state: "stopping"; readonly runId: string };

interface RunFormProps {
  readonly claudeModel: string;
  readonly mode: PolicyMode;
  readonly profiles: readonly PolicyProfileView[];
  readonly replaySources: readonly ReplaySourceView[];
  readonly replaySourcesLoading: boolean;
  readonly seed: string;
  readonly sourceRunId: string;
  readonly control: RunControl;
  readonly onModeChange: (mode: PolicyMode) => void;
  readonly onClaudeModelChange: (model: string) => void;
  readonly onSeedChange: (seed: string) => void;
  readonly onSourceRunIdChange: (runId: string) => void;
  readonly onRun: () => void;
  readonly onStop: (runId: string) => void;
}

export function RunForm({
  claudeModel,
  mode,
  profiles,
  replaySources,
  replaySourcesLoading,
  seed,
  sourceRunId,
  control,
  onClaudeModelChange,
  onModeChange,
  onSeedChange,
  onSourceRunIdChange,
  onRun,
  onStop,
}: RunFormProps) {
  const selectedProfile = profiles.find((profile) => profile.mode === mode);
  const formLocked = control.state !== "idle";
  const canStart =
    Boolean(selectedProfile?.available) &&
    (mode !== "replay" || Boolean(sourceRunId)) &&
    (mode !== "claude" || Boolean(claudeModel));

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
          onChange={(event) => onModeChange(event.target.value as PolicyMode)}
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
        </select>
      </label>

      {mode === "replay" ? (
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

      {mode === "claude" && selectedProfile && (
        <label className="run-field model-field">
          <span>Claude model</span>
          <select
            disabled={formLocked || selectedProfile.models.length === 0}
            name="claudeModel"
            onChange={(event) => onClaudeModelChange(event.target.value)}
            required
            value={claudeModel}
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
      <RunActionButton
        canStart={canStart}
        control={control}
        exactReplay={mode === "replay"}
        onStop={onStop}
      />
      {selectedProfile && (
        <span className="profile-hint" title={selectedProfile.description}>
          {mode === "replay"
            ? "Creates a new deterministic run from a completed source; no agents are called."
            : mode === "claude" && claudeModel
            ? [selectedProfile.provider, claudeModel].filter(Boolean).join(" / ")
            : selectedProfile.model
            ? [
                selectedProfile.provider,
                selectedProfile.model,
                selectedProfile.reasoningEffort,
              ]
                .filter(Boolean)
                .join(" · ")
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
  readonly control: RunControl;
  readonly exactReplay: boolean;
  readonly onStop: (runId: string) => void;
}

function RunActionButton({
  canStart,
  control,
  exactReplay,
  onStop,
}: RunActionButtonProps) {
  if (control.state === "running") {
    return (
      <button
        className="run-button stop-run-button"
        onClick={() => confirmStop(control.runId, onStop)}
        type="button"
      >
        <span aria-hidden="true">■</span>
        Stop run
      </button>
    );
  }

  if (control.state === "starting" || control.state === "stopping") {
    return (
      <button className="run-button" disabled type="button">
        <span aria-hidden="true" className="spinner" />
        {control.state === "starting" ? "Starting…" : "Stopping…"}
      </button>
    );
  }

  return (
    <button className="run-button" disabled={!canStart} type="submit">
      <span aria-hidden="true">▶</span>
      {exactReplay ? "Start exact replay" : "Run 30 days"}
    </button>
  );
}

function confirmStop(runId: string, onStop: (runId: string) => void): void {
  if (
    window.confirm(
      "Stop this run? Recorded activity will remain available, but the run cannot be resumed.",
    )
  ) {
    onStop(runId);
  }
}

function replaySourceLabel(source: ReplaySourceView): string {
  return `${shortRunId(source.runId)} · ${formatDateTime(source.submittedAt)}`;
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
