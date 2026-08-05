import { useEffect, useMemo, useRef, useState } from "react";

import { companyLabel } from "../domainLabels";
import { formatExactDecimal, formatValue } from "../format";
import { isAbortError, requestErrorMessage } from "../requestErrors";
import {
  ACCEPTED_BY_ENGINE_LABEL,
  clockTime,
  commandDispositionLabel,
  commandLabel,
  commandProcessingResult,
  commandSummary,
  decisionContextSummary,
  nextDecisionTiming,
  plural,
  systemLabel,
  wakeLabel,
} from "../timelineFormatters";
import type {
  SystemTimelineItemView,
  TimelineCommandView,
  TimelineContextView,
  TimelineDayView,
  TimelineDetailView,
  TimelineMomentView,
  TurnTimelineItemView,
} from "../types";
import { DecisionDrawer } from "./DecisionDrawer";
import { MarketDisplay } from "./MarketDisplay";
import { TimelineError, TimelineNotice } from "./TimelineFeedback";

type TimelineLoader = (
  runId: string,
  day: number,
  signal?: AbortSignal,
) => Promise<TimelineDayView>;
type DetailLoader = (
  runId: string,
  entryId: string,
  signal?: AbortSignal,
) => Promise<TimelineDetailView>;
type StatusFilter = "accepted" | "all" | "rejected";

interface OperationsTimelineProps {
  readonly day: number;
  readonly days: number;
  readonly loadDetail: DetailLoader;
  readonly loadTimeline: TimelineLoader;
  readonly onDayChange: (day: number) => void;
  readonly revision: string;
  readonly runId: string;
  readonly timelinePending?: boolean;
}

interface CompanyOption {
  readonly companyId: string;
  readonly companyName: string;
}

interface TimelineFilters {
  readonly command: TimelineCommandView["kind"] | typeof ALL;
  readonly company: string;
  readonly status: StatusFilter;
}

const ALL = "all";
const INITIAL_FILTERS: TimelineFilters = {
  command: ALL,
  company: ALL,
  status: "all",
};

export function OperationsTimeline({
  day,
  days,
  loadDetail,
  loadTimeline,
  onDayChange,
  revision,
  runId,
  timelinePending = false,
}: OperationsTimelineProps) {
  const [timeline, setTimeline] = useState<TimelineDayView | null>(null);
  const [filters, setFilters] = useState<TimelineFilters>(INITIAL_FILTERS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedEntryId, setSelectedEntryId] = useState<string | null>(null);
  const dayCache = useRef(new Map<string, TimelineDayView>());

  useEffect(() => {
    dayCache.current.clear();
    setTimeline(null);
    setSelectedEntryId(null);
    setFilters(INITIAL_FILTERS);
  }, [runId]);

  useEffect(() => {
    if (timelinePending) {
      setTimeline(null);
      setLoading(false);
      setError(null);
      return;
    }

    const cacheKey = `${revision}:${day}`;
    const cached = dayCache.current.get(cacheKey);
    if (cached) {
      setTimeline(cached);
      setLoading(false);
      setError(null);
      return;
    }

    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void loadTimeline(runId, day, controller.signal)
      .then((nextTimeline) => {
        dayCache.current.set(cacheKey, nextTimeline);
        setTimeline(nextTimeline);
      })
      .catch((reason: unknown) => {
        if (!isAbortError(reason)) {
          setError(
            requestErrorMessage(reason, {
              fallback:
                "This run has not produced a readable operations timeline yet.",
            }),
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [day, loadTimeline, revision, runId, timelinePending]);

  const companies = useMemo<readonly CompanyOption[]>(
    () => timelineCompanies(timeline),
    [timeline],
  );

  const commandKinds = useMemo<readonly TimelineCommandView["kind"][]>(
    () =>
      [...new Set(timeline?.moments.flatMap((moment) =>
        moment.turns.map((turn) => turn.command.kind),
      )) ?? []].sort(),
    [timeline],
  );
  const visibleMoments = useMemo(
    () => filterMoments(timeline?.moments ?? [], filters),
    [filters, timeline],
  );

  return (
    <section aria-labelledby="operations-title" className="panel operations-panel">
      <OperationsHeader
        context={timeline?.context ?? null}
        day={day}
        days={days}
      />

      {timeline && (
        <>
          <RunProvenanceBanner context={timeline.context} />
          <DayNavigator
            day={day}
            onSelect={onDayChange}
            summaries={timeline.daySummaries}
          />
          <TimelineFilterBar
            commandKinds={commandKinds}
            companies={companies}
            filters={filters}
            onChange={setFilters}
          />
        </>
      )}

      {timelinePending ? (
        <TimelineNotice label="This run is queued and has not produced timeline data yet." />
      ) : loading ? (
        <TimelineNotice label="Loading this simulation day…" />
      ) : error ? (
        <TimelineError message={error} />
      ) : visibleMoments.length === 0 ? (
        <TimelineNotice label="No timeline entries match these filters." />
      ) : (
        <div className="operations-stream">
          {visibleMoments.map((moment) => (
            <MinuteBucket
              key={moment.simMinute}
              moment={moment}
              onSelectEntry={setSelectedEntryId}
            />
          ))}
        </div>
      )}

      {selectedEntryId && (
        <DecisionDrawer
          entryId={selectedEntryId}
          loadDetail={loadDetail}
          onClose={() => setSelectedEntryId(null)}
          onSelectEntry={setSelectedEntryId}
          runId={runId}
        />
      )}
    </section>
  );
}

function OperationsHeader({
  context,
  day,
  days,
}: {
  readonly context: TimelineContextView | null;
  readonly day: number;
  readonly days: number;
}) {
  return (
    <div className="section-heading operations-heading">
      <div>
        <span className="eyebrow">OPERATIONS TIMELINE</span>
        <h2 id="operations-title">
          Company decisions and economic activity, minute by minute
        </h2>
      </div>
      <p>
        Day {day} of {context?.totalDays ?? days}
        {context ? checkpointSummary(context) : ""}
      </p>
    </div>
  );
}

function checkpointSummary(context: TimelineContextView): string {
  const minute = context.checkpointMinute;
  if (minute === null) {
    return "";
  }
  const day = Math.floor(minute / (24 * 60)) + 1;
  return ` · last checkpoint Day ${day} ${clockTime(minute)} · state v${context.checkpointStateVersion}`;
}

function RunProvenanceBanner({ context }: { readonly context: TimelineContextView }) {
  if (!context.isReplay) {
    return (
      <div className="trace-provenance live">
        <span className="provenance-icon" aria-hidden="true">
          LIVE
        </span>
        <div>
          <strong>{context.currentModelCallCount} model calls in this run</strong>
          <p>
            Trace material and {formatValue(context.currentUsage.totalTokens)}{" "}
            tokens belong to run <code>{context.currentRunId}</code>.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="trace-provenance replay">
      <span className="provenance-icon" aria-hidden="true">
        REPLAY
      </span>
      <div>
        <strong>
          Zero-call replay · {context.currentModelCallCount} current model calls ·{" "}
          {formatValue(context.currentUsage.totalTokens)} current tokens
        </strong>
        <p>
          Direct replay parent:{" "}
          <code>{context.sourceRunId ?? "not recorded"}</code>. Ultimate trace
          source:{" "}
          <code>{context.traceRunId}</code>:{" "}
          {context.sourceModelCallCount} calls and{" "}
          {formatValue(context.sourceUsage.totalTokens)} tokens. Source usage is
          never charged to this replay.
        </p>
      </div>
    </div>
  );
}

function DayNavigator({
  day,
  onSelect,
  summaries,
}: {
  readonly day: number;
  readonly onSelect: (day: number) => void;
  readonly summaries: TimelineDayView["daySummaries"];
}) {
  return (
    <nav aria-label="Simulation day" className="day-navigator">
      <button
        aria-label="Previous day"
        disabled={day <= 1}
        onClick={() => onSelect(day - 1)}
        type="button"
      >
        ←
      </button>
      <div className="day-density-strip">
        {summaries.map((summary) => (
          <button
            aria-current={summary.day === day ? "page" : undefined}
            className={summary.day === day ? "selected" : ""}
            key={summary.day}
            onClick={() => onSelect(summary.day)}
            style={{
              "--activity": activityOpacity(summary.turnCount, summaries),
            } as React.CSSProperties}
            title={`Day ${summary.day}: ${summary.turnCount} turns, ${summary.rejectedCount} rejected, ${formatExactDecimal(summary.tradeQuantity)} traded`}
            type="button"
          >
            <span>{summary.day}</span>
          </button>
        ))}
      </div>
      <button
        aria-label="Next day"
        disabled={day >= summaries.length}
        onClick={() => onSelect(day + 1)}
        type="button"
      >
        →
      </button>
    </nav>
  );
}

function TimelineFilterBar({
  commandKinds,
  companies,
  filters,
  onChange,
}: {
  readonly commandKinds: readonly TimelineCommandView["kind"][];
  readonly companies: readonly CompanyOption[];
  readonly filters: TimelineFilters;
  readonly onChange: (filters: TimelineFilters) => void;
}) {
  return (
    <div className="timeline-filters">
      <label>
        <span>Company</span>
        <select
          onChange={(event) =>
            onChange({ ...filters, company: event.target.value })
          }
          value={filters.company}
        >
          <option value={ALL}>All companies</option>
          {companies.map((company) => (
            <option key={company.companyId} value={company.companyId}>
              {company.companyName}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>Status</span>
        <select
          onChange={(event) =>
            onChange({
              ...filters,
              status: event.target.value as StatusFilter,
            })
          }
          value={filters.status}
        >
          <option value="all">All Results</option>
          <option value="accepted">{ACCEPTED_BY_ENGINE_LABEL}</option>
          <option value="rejected">Rejected Commands</option>
        </select>
      </label>
      <label>
        <span>Command</span>
        <select
          onChange={(event) =>
            onChange({
              ...filters,
              command: event.target.value as TimelineFilters["command"],
            })
          }
          value={filters.command}
        >
          <option value={ALL}>All commands</option>
          {commandKinds.map((kind) => (
            <option key={kind} value={kind}>
              {commandLabel(kind)}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

function MinuteBucket({
  moment,
  onSelectEntry,
}: {
  readonly moment: TimelineMomentView;
  readonly onSelectEntry: (entryId: string) => void;
}) {
  return (
    <article className="minute-bucket">
      <header className="minute-marker">
        <time>{clockTime(moment.simMinute)}</time>
        <span>
          {moment.totalTurnCount > 1
            ? concurrencyLabel(moment)
            : moment.turns.length === 1
              ? "1 company turn"
              : "System event"}
        </span>
      </header>
      <div className="minute-content">
        <div className="minute-lanes">
          <section className="company-activity-lane" aria-label="Company activity">
            {moment.systemSteps.map((step) => (
              <SystemStepCard
                key={step.entryId}
                onSelect={() => onSelectEntry(step.entryId)}
                step={step}
              />
            ))}
            {moment.totalTurnCount > 1 && (
              <div className="concurrency-note">
                <span>CONCURRENT OBSERVATION</span>
                Agents saw the same pre-apply state. Cards below follow the persisted
                seed-derived apply order.
              </div>
            )}
            <div className="turn-card-grid">
              {moment.turns.map((turn) => (
                <TurnCard
                  key={turn.entryId}
                  onSelect={() => onSelectEntry(turn.entryId)}
                  turn={turn}
                />
              ))}
            </div>
          </section>
          <MarketDisplay frame={moment.market} minute={moment.simMinute} />
        </div>
      </div>
    </article>
  );
}

function SystemStepCard({
  onSelect,
  step,
}: {
  readonly onSelect: () => void;
  readonly step: SystemTimelineItemView;
}) {
  return (
    <button className="system-step-card" onClick={onSelect} type="button">
      <span className="system-step-icon" aria-hidden="true">
        SYSTEM
      </span>
      <span>
        <small>{systemLabel(step.kind)}</small>
        <strong>{step.title}</strong>
        <span>{step.summary}</span>
      </span>
      <span className="system-step-meta">
        {step.effects.length} {plural(step.effects.length, "effect")}
        {" · "}
        {step.affectedCompanyIds.length > 0
          ? step.affectedCompanyIds.map((id) => companyLabel(id)).join(", ")
          : "No direct economic impact"}
      </span>
    </button>
  );
}

function TurnCard({
  onSelect,
  turn,
}: {
  readonly onSelect: () => void;
  readonly turn: TurnTimelineItemView;
}) {
  return (
    <article
      aria-label={`${turn.companyName} turn at ${clockTime(turn.simMinute)}`}
      className={`turn-card ${turn.accepted ? "accepted" : "rejected"}`}
    >
      <header>
        <span className={`role-dot ${turn.role}`} />
        <span>
          <strong>{turn.companyName}</strong>
          <small>{decisionContextSummary(turn)}</small>
        </span>
        <span className={`decision-status ${turn.accepted ? "accepted" : "rejected"}`}>
          {commandDispositionLabel(turn)}
        </span>
      </header>
      <ol className="decision-chain">
        <DecisionStage
          label="Decision Trigger"
          value={turn.wakeSignals
            .map((signal) => wakeLabel(signal.reason))
            .join(" · ")}
        />
        <DecisionStage
          label="Company Command"
          value={commandSummary(turn.command)}
        />
        <DecisionStage
          label="Command Processing Result"
          tone={turn.accepted ? "positive" : "negative"}
          value={commandProcessingResult(turn)}
        />
        <DecisionStage
          label="Next Decision Timing"
          value={nextDecisionTiming(turn)}
        />
      </ol>
      <footer>
        <span>{turn.title}</span>
        {turn.sourceTurnId && <span>Source turn linked</span>}
        <button
          aria-label={`Open ${turn.companyName} turn detail at ${clockTime(turn.simMinute)}`}
          className="turn-card-action"
          onClick={onSelect}
          type="button"
        >
          Open detail →
        </button>
      </footer>
    </article>
  );
}

function DecisionStage({
  label,
  tone,
  value,
}: {
  readonly label: string;
  readonly tone?: "negative" | "positive";
  readonly value: string;
}) {
  return (
    <li className={tone ? `stage-${tone}` : undefined}>
      <span>{label}</span>
      <strong>{value}</strong>
    </li>
  );
}

function filterMoments(
  moments: readonly TimelineMomentView[],
  filters: TimelineFilters,
): readonly TimelineMomentView[] {
  return moments.flatMap((moment) => {
    const systemSteps = moment.systemSteps.filter(
      (step) => step.kind !== "agent_wake_suppressed",
    );
    const turns = moment.turns.filter((turn) => {
      return (
        (filters.company === ALL || turn.companyId === filters.company) &&
        (filters.command === ALL || turn.command.kind === filters.command) &&
        (filters.status === "all" ||
          (filters.status === "accepted" && turn.accepted) ||
          (filters.status === "rejected" && !turn.accepted))
      );
    });
    if (turns.length === 0 && systemSteps.length === 0) {
      return [];
    }
    return [{ ...moment, systemSteps, turns }];
  });
}

function timelineCompanies(
  timeline: TimelineDayView | null,
): readonly CompanyOption[] {
  const companies = new Map<string, CompanyOption>();
  timeline?.moments.forEach((moment) =>
    moment.turns.forEach((turn) =>
      companies.set(turn.companyId, {
        companyId: turn.companyId,
        companyName: turn.companyName,
      }),
    ),
  );
  return [...companies.values()].sort((left, right) =>
    left.companyName.localeCompare(right.companyName),
  );
}

function activityOpacity(
  count: number,
  summaries: TimelineDayView["daySummaries"],
): string {
  const maximum = Math.max(1, ...summaries.map((summary) => summary.turnCount));
  return `${(18 + (count / maximum) * 82).toFixed(1)}%`;
}

function sharedStateVersion(moment: TimelineMomentView): string {
  const versions = [...new Set(moment.turns.map((turn) => turn.stateVersion))];
  return versions.length === 1 ? String(versions[0]) : versions.join("/");
}

function concurrencyLabel(moment: TimelineMomentView): string {
  const state = `state v${sharedStateVersion(moment)}`;
  return moment.turns.length === moment.totalTurnCount
    ? `${moment.totalTurnCount} companies observed ${state} concurrently`
    : `${moment.turns.length} of ${moment.totalTurnCount} concurrent turns shown · observed ${state}`;
}
