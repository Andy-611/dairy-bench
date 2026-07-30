import type { FormEvent } from "react";

import type { PolicyMode, PolicyProfileView } from "../types";

const MODE_LABELS: Readonly<Record<PolicyMode, string>> = {
  baseline: "Rule baseline",
  codex: "Codex agents",
  openai: "OpenAI agents",
  replay: "Historical replay",
};

interface RunFormProps {
  readonly mode: PolicyMode;
  readonly profiles: readonly PolicyProfileView[];
  readonly seed: string;
  readonly sourceRunId: string;
  readonly isLoading: boolean;
  readonly onModeChange: (mode: PolicyMode) => void;
  readonly onSeedChange: (seed: string) => void;
  readonly onSourceRunIdChange: (runId: string) => void;
  readonly onRun: () => void;
}

export function RunForm({
  mode,
  profiles,
  seed,
  sourceRunId,
  isLoading,
  onModeChange,
  onSeedChange,
  onSourceRunIdChange,
  onRun,
}: RunFormProps) {
  const selectedProfile = profiles.find((profile) => profile.mode === mode);

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    onRun();
  }

  return (
    <form className="run-form" onSubmit={handleSubmit}>
      <label className="run-field mode-field">
        <span>Company policy</span>
        <select
          disabled={isLoading}
          onChange={(event) => onModeChange(event.target.value as PolicyMode)}
          value={mode}
        >
          {profiles.map((profile) => (
            <option
              disabled={!profile.available}
              key={profile.mode}
              value={profile.mode}
            >
              {MODE_LABELS[profile.mode]}
              {!profile.available ? " (not configured)" : ""}
            </option>
          ))}
        </select>
      </label>

      {mode === "replay" ? (
        <label className="run-field replay-field">
          <span>Source run ID</span>
          <input
            autoComplete="off"
            disabled={isLoading}
            name="sourceRunId"
            onChange={(event) => onSourceRunIdChange(event.target.value)}
            placeholder="Paste a completed run ID"
            required
            type="text"
            value={sourceRunId}
          />
        </label>
      ) : (
        <label className="run-field seed-field">
          <span>Random seed</span>
          <input
            aria-describedby="seed-help"
            autoComplete="off"
            disabled={isLoading}
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

      <span className="sr-only" id="seed-help">
        Use the same seed to reproduce the economic environment.
      </span>
      <button
        className="run-button"
        disabled={isLoading || !selectedProfile?.available}
        type="submit"
      >
        {isLoading ? (
          <>
            <span aria-hidden="true" className="spinner" />
            Running
          </>
        ) : (
          <>
            <span aria-hidden="true">▶</span>
            {mode === "replay" ? "Replay run" : "Run 30 days"}
          </>
        )}
      </button>
      {selectedProfile && (
        <span className="profile-hint" title={selectedProfile.description}>
          {selectedProfile.model
            ? [selectedProfile.provider, selectedProfile.model]
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
