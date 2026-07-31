import { useEffect, useRef, useState } from "react";

import { DairyBenchApi } from "./api";
import { CompanyTable } from "./components/CompanyTable";
import { EmptyState } from "./components/EmptyState";
import { MetricChart } from "./components/MetricChart";
import { OperationsReplay } from "./components/OperationsReplay";
import { RunForm } from "./components/RunForm";
import { SummaryCards } from "./components/SummaryCards";
import { TokenSummary } from "./components/TokenSummary";
import { isAbortError, requestErrorMessage } from "./requestErrors";
import type {
  EpisodeView,
  PolicyMode,
  PolicyProfileView,
  RunProgressView,
  RunRequest,
} from "./types";

const api = new DairyBenchApi();
const MAX_SEED = 2_147_483_647;

export function App() {
  const [mode, setMode] = useState<PolicyMode>("baseline");
  const [profiles, setProfiles] = useState<readonly PolicyProfileView[]>([]);
  const [seed, setSeed] = useState("42");
  const [sourceRunId, setSourceRunId] = useState("");
  const [progress, setProgress] = useState<RunProgressView | null>(null);
  const [result, setResult] = useState<EpisodeView | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const activeRequest = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void api
      .policyProfiles(controller.signal)
      .then(setProfiles)
      .catch((reason: unknown) => {
        if (!isAbortError(reason)) {
          setError(
            requestErrorMessage(reason, {
              fallback: "The backend policy profiles could not be loaded.",
            }),
          );
        }
      });

    return () => {
      controller.abort();
      activeRequest.current?.abort();
    };
  }, []);

  async function runBenchmark(): Promise<void> {
    const request = buildRunRequest(mode, seed, sourceRunId);
    if (typeof request === "string") {
      setError(request);
      return;
    }

    const controller = new AbortController();
    activeRequest.current?.abort();
    activeRequest.current = controller;
    setError(null);
    setProgress(null);
    setIsLoading(true);

    try {
      const nextResult = await api.run(request, setProgress, controller.signal);
      setResult(nextResult);
    } catch (reason: unknown) {
      if (isAbortError(reason)) {
        return;
      }
      setError(
        requestErrorMessage(reason, {
          fallback:
            "The run failed. Confirm that the backend service is running.",
          network:
            "Could not reach the backend. Confirm FastAPI is running at 127.0.0.1:8000.",
        }),
      );
    } finally {
      if (activeRequest.current === controller) {
        activeRequest.current = null;
        setIsLoading(false);
      }
    }
  }

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
          isLoading={isLoading}
          mode={mode}
          onModeChange={setMode}
          onRun={() => void runBenchmark()}
          onSeedChange={setSeed}
          onSourceRunIdChange={setSourceRunId}
          profiles={profiles}
          seed={seed}
          sourceRunId={sourceRunId}
        />
      </header>

      <main>
        <section className="hero">
          <div>
            <span className="eyebrow">
              {(result?.scenarioId ?? "flow.dairy.base.s12.v2").toUpperCase()}
            </span>
            <h1>Independent companies. One living dairy economy.</h1>
            <p>
              Each company uses an independent agent to make event-driven,
              atomic decisions across production, two spot markets,
              processing, retail, and perishable inventory settlement.
            </p>
          </div>
          {result && (
            <dl className="run-meta">
              <div>
                <dt>Run</dt>
                <dd>{result.runId}</dd>
              </div>
              <div>
                <dt>Seed</dt>
                <dd>{result.seed}</dd>
              </div>
              <div>
                <dt>Horizon</dt>
                <dd>{result.days} days</dd>
              </div>
            </dl>
          )}
        </section>

        <div aria-live="polite">
          {error && (
            <div className="error-banner" role="alert">
              <span aria-hidden="true">!</span>
              <div>
                <strong>The run could not be completed</strong>
                <p>{error}</p>
              </div>
              <button onClick={() => setError(null)} type="button">
                Dismiss
              </button>
            </div>
          )}
          {isLoading && (
            <div className="loading-banner" role="status">
              <span className="spinner dark" />
              <div className="progress-copy">
                <span>{progressText(progress)}</span>
                <progress
                  max={progress?.totalDays ?? 30}
                  value={progress?.currentDay ?? 0}
                />
              </div>
            </div>
          )}
        </div>

        {!result ? (
          <EmptyState />
        ) : (
          <div className="dashboard">
            <SummaryCards score={result.score} />
            {result.agentUsage && <TokenSummary summary={result.agentUsage} />}
            <CompanyTable companies={result.companies} />
            <MetricChart snapshots={result.snapshots} />
            <OperationsReplay
              companies={result.companies}
              days={result.days}
              key={result.runId}
              loadDetail={loadTimelineDetail}
              loadTimeline={loadTimelineDay}
              runId={result.runId}
            />
          </div>
        )}
      </main>

      <footer>
        <span>Dairy Bench V2</span>
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
    const normalizedRunId = sourceRunId.trim();
    return normalizedRunId
      ? { policyMode: mode, sourceRunId: normalizedRunId }
      : "Replay mode requires a source run ID.";
  }

  const parsedSeed = Number(seed);
  return Number.isInteger(parsedSeed) &&
    parsedSeed >= 0 &&
    parsedSeed <= MAX_SEED
    ? { policyMode: mode, seed: parsedSeed }
    : `The random seed must be an integer from 0 to ${MAX_SEED}.`;
}

function progressText(progress: RunProgressView | null): string {
  if (!progress || progress.status === "queued") {
    return "Run queued. Preparing independent company agents…";
  }
  if (progress.status === "interrupted") {
    return "Resuming an interrupted run…";
  }
  return `Running day ${progress.currentDay} of ${progress.totalDays}`;
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
