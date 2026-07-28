import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, DairyBenchApi } from "./api";
import { CompanyTable } from "./components/CompanyTable";
import { EmptyState } from "./components/EmptyState";
import { EventTable } from "./components/EventTable";
import { MetricChart } from "./components/MetricChart";
import { RunForm } from "./components/RunForm";
import { SummaryCards } from "./components/SummaryCards";
import { TokenSummary } from "./components/TokenSummary";
import type {
  EpisodeView,
  InvocationArtifactsView,
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
  const artifactCache = useRef(
    new Map<
      string,
      Map<string, Promise<InvocationArtifactsView | null>>
    >(),
  );

  useEffect(() => {
    const controller = new AbortController();
    void api
      .policyProfiles(controller.signal)
      .then(setProfiles)
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) {
          setError(
            reason instanceof Error
              ? reason.message
              : "无法读取后端的策略配置。",
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
      artifactCache.current.clear();
      artifactCache.current.set(nextResult.runId, new Map());
      setResult(nextResult);
    } catch (reason: unknown) {
      if (reason instanceof DOMException && reason.name === "AbortError") {
        return;
      }
      setError(
        reason instanceof TypeError
          ? "无法连接后端服务，请确认 FastAPI 已在 127.0.0.1:8000 启动。"
          : reason instanceof Error
            ? reason.message
            : "运行失败，请确认后端服务已经启动。",
      );
    } finally {
      if (activeRequest.current === controller) {
        activeRequest.current = null;
        setIsLoading(false);
      }
    }
  }

  const loadInvocationArtifacts = useCallback(
    (
      runId: string,
      invocationId: string,
    ): Promise<InvocationArtifactsView | null> => {
      let runCache = artifactCache.current.get(runId);
      if (!runCache) {
        runCache = new Map();
        artifactCache.current.set(runId, runCache);
      }

      const cached = runCache.get(invocationId);
      if (cached) {
        return cached;
      }

      const pending = api
        .invocationArtifacts(runId, invocationId)
        .catch((reason: unknown) => {
          if (reason instanceof ApiError && reason.status === 404) {
            return null;
          }
          throw reason;
        });
      runCache.set(invocationId, pending);
      void pending.catch(() => {
        if (runCache?.get(invocationId) === pending) {
          runCache.delete(invocationId);
        }
      });
      return pending;
    },
    [],
  );

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span aria-hidden="true" className="brand-mark">
            D
          </span>
          <span>
            <strong>Dairy Bench</strong>
            <small>单 Agent 公司 · 多公司鲜奶产业链</small>
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
            <span className="eyebrow">FLOW.DAIRY.BASE.S6.V1</span>
            <h1>让六家公司共同经营一条鲜奶产业链</h1>
            <p>
              每家公司由一个独立 Agent 决策。30
              天内完成原奶生产、两级现货交易、加工、零售与库存过期结算，
              同时观察整个经济系统的效率与公平。
            </p>
          </div>
          {result && (
            <dl className="run-meta">
              <div>
                <dt>本次运行</dt>
                <dd>{result.runId}</dd>
              </div>
              <div>
                <dt>随机种子</dt>
                <dd>{result.seed}</dd>
              </div>
              <div>
                <dt>运行周期</dt>
                <dd>{result.days} 天</dd>
              </div>
            </dl>
          )}
        </section>

        <div aria-live="polite">
          {error && (
            <div className="error-banner" role="alert">
              <span aria-hidden="true">!</span>
              <div>
                <strong>暂时无法完成运行</strong>
                <p>{error}</p>
              </div>
              <button onClick={() => setError(null)} type="button">
                关闭
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
            <EventTable
              events={result.events}
              invocations={result.invocations}
              key={result.runId}
              loadArtifacts={loadInvocationArtifacts}
              runId={result.runId}
            />
          </div>
        )}
      </main>

      <footer>
        <span>Dairy Bench V1</span>
        <span>API Key 与 Codex 登录凭证只由后端使用，不会进入浏览器。</span>
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
      : "复放模式需要填写来源 Run ID。";
  }

  const parsedSeed = Number(seed);
  return Number.isInteger(parsedSeed) &&
    parsedSeed >= 0 &&
    parsedSeed <= MAX_SEED
    ? { policyMode: mode, seed: parsedSeed }
    : `随机种子必须是 0 到 ${MAX_SEED} 之间的整数。`;
}

function progressText(progress: RunProgressView | null): string {
  if (!progress || progress.status === "queued") {
    return "运行已进入队列，正在准备六家公司……";
  }
  if (progress.status === "interrupted") {
    return "上次运行被中断，后端正在恢复……";
  }
  return `正在运行第 ${progress.currentDay} / ${progress.totalDays} 天`;
}
