import { useEffect, useMemo, useRef, useState } from "react";

import { companyLabel } from "../domainLabels";
import { formatValue } from "../format";
import { isAbortError, requestErrorMessage } from "../requestErrors";
import {
  clockTime,
  commandLabel,
  commandSummary,
  effectSummary,
  plural,
  stateChangeSummary,
  systemLabel,
  wakeLabel,
} from "../timelineFormatters";
import type {
  CompanyResultView,
  SystemTimelineItemView,
  TimelineCommandView,
  TimelineContextView,
  TimelineDayView,
  TimelineDetailView,
  TimelineMomentView,
  TurnTimelineItemView,
} from "../types";
import { DecisionDrawer } from "./DecisionDrawer";
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

interface OperationsReplayProps {
  readonly companies: readonly CompanyResultView[];
  readonly days: number;
  readonly loadDetail: DetailLoader;
  readonly loadTimeline: TimelineLoader;
  readonly runId: string;
}

interface TimelineFilters {
  readonly command: TimelineCommandView["kind"] | typeof ALL;
  readonly company: string;
  readonly showNoEffectWaits: boolean;
  readonly status: StatusFilter;
}

const ALL = "all";
const INITIAL_FILTERS: TimelineFilters = {
  command: ALL,
  company: ALL,
  showNoEffectWaits: false,
  status: "all",
};

export function OperationsReplay({
  companies,
  days,
  loadDetail,
  loadTimeline,
  runId,
}: OperationsReplayProps) {
  const [selectedDay, setSelectedDay] = useState(1);
  const [timeline, setTimeline] = useState<TimelineDayView | null>(null);
  const [filters, setFilters] = useState<TimelineFilters>(INITIAL_FILTERS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedEntryId, setSelectedEntryId] = useState<string | null>(null);
  const dayCache = useRef(new Map<number, TimelineDayView>());

  useEffect(() => {
    dayCache.current.clear();
    setSelectedDay(1);
    setSelectedEntryId(null);
    setFilters(INITIAL_FILTERS);
  }, [runId]);

  useEffect(() => {
    const cached = dayCache.current.get(selectedDay);
    if (cached) {
      setTimeline(cached);
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void loadTimeline(runId, selectedDay, controller.signal)
      .then((nextTimeline) => {
        dayCache.current.set(selectedDay, nextTimeline);
        setTimeline(nextTimeline);
      })
      .catch((reason: unknown) => {
        if (!isAbortError(reason)) {
          setError(
            requestErrorMessage(reason, {
              fallback: "The operations timeline could not be loaded.",
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
  }, [loadTimeline, runId, selectedDay]);

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
  const hiddenWaitCount = useMemo(
    () =>
      filters.showNoEffectWaits
        ? 0
        : (timeline?.moments ?? []).reduce(
            (total, moment) =>
              total +
              moment.turns.filter(isCollapsibleWait).length,
            0,
          ),
    [filters.showNoEffectWaits, timeline],
  );

  return (
    <section aria-labelledby="operations-title" className="panel operations-panel">
      <OperationsHeader
        context={timeline?.context ?? null}
        day={selectedDay}
        days={days}
        hiddenWaitCount={hiddenWaitCount}
      />

      {timeline && (
        <>
          <RunProvenanceBanner context={timeline.context} />
          <DayNavigator
            day={selectedDay}
            onSelect={setSelectedDay}
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

      {loading ? (
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
  hiddenWaitCount,
}: {
  readonly context: TimelineContextView | null;
  readonly day: number;
  readonly days: number;
  readonly hiddenWaitCount: number;
}) {
  return (
    <div className="section-heading operations-heading">
      <div>
        <span className="eyebrow">TURN-FIRST OPERATIONS REPLAY</span>
        <h2 id="operations-title">Wake, decide, apply, observe</h2>
      </div>
      <p>
        Day {day} of {context?.totalDays ?? days}
        {hiddenWaitCount > 0
          ? ` · ${hiddenWaitCount} no-effect waits collapsed`
          : " · every matching turn is visible"}
      </p>
    </div>
  );
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
            title={`Day ${summary.day}: ${summary.turnCount} turns, ${summary.rejectedCount} rejected, ${formatValue(summary.tradeQuantity)} traded`}
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
  readonly companies: readonly CompanyResultView[];
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
          <option value="all">Accepted and rejected</option>
          <option value="accepted">Accepted only</option>
          <option value="rejected">Rejected only</option>
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
      <label className="wait-toggle">
        <input
          checked={filters.showNoEffectWaits}
          onChange={(event) =>
            onChange({
              ...filters,
              showNoEffectWaits: event.target.checked,
            })
          }
          type="checkbox"
        />
        <span>Show no-effect waits</span>
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
              : "System transition"}
        </span>
      </header>
      <div className="minute-content">
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
            Agents saw the same pre-apply state. Cards below follow deterministic
            apply order.
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
        SYS
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
        {step.reconstructed && " · reconstructed from the source journal"}
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
  const effects = turn.effects.map(effectSummary);
  const changes = turn.stateChanges.map(stateChangeSummary);
  return (
    <article
      aria-label={`${turn.companyName} turn at ${clockTime(turn.simMinute)}`}
      className={`turn-card ${turn.accepted ? "accepted" : "rejected"}`}
    >
      <header>
        <span className={`role-dot ${turn.role}`} />
        <span>
          <strong>{turn.companyName}</strong>
          <small>
            Observed v{turn.stateVersion} · Apply #{turn.applySequence}
          </small>
        </span>
        <span className={`decision-status ${turn.accepted ? "accepted" : "rejected"}`}>
          {turn.accepted ? "Accepted" : "Rejected"}
        </span>
      </header>
      <ol className="decision-chain">
        <DecisionStage
          label="Wake"
          value={turn.wakeSignals
            .map((signal) => wakeLabel(signal.reason))
            .join(" · ")}
        />
        <DecisionStage
          label="Command"
          value={commandSummary(turn.command)}
        />
        <DecisionStage
          label="Outcome"
          tone={turn.accepted ? "positive" : "negative"}
          value={
            turn.reason ??
            (effects.length + changes.length > 0
              ? [...changes, ...effects].join(" · ")
              : "No immediate economic change")
          }
        />
        <DecisionStage
          label="Next"
          value={
            turn.nextAvailableMinute === null
              ? "No scheduled continuation"
              : `Available at ${clockTime(turn.nextAvailableMinute)}`
          }
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
    const turns = moment.turns.filter((turn) => {
      return (
        (filters.company === ALL || turn.companyId === filters.company) &&
        (filters.command === ALL || turn.command.kind === filters.command) &&
        (filters.status === "all" ||
          (filters.status === "accepted" && turn.accepted) ||
          (filters.status === "rejected" && !turn.accepted)) &&
        (filters.showNoEffectWaits || !isCollapsibleWait(turn))
      );
    });
    if (turns.length === 0 && moment.systemSteps.length === 0) {
      return [];
    }
    return [{ ...moment, turns }];
  });
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

function isCollapsibleWait(turn: TurnTimelineItemView): boolean {
  return (
    turn.accepted &&
    turn.command.kind === "wait" &&
    turn.command.untilMinute === null &&
    turn.effects.length === 0 &&
    turn.stateChanges.length === 0 &&
    turn.nextAvailableMinute === null &&
    turn.protocolError === null
  );
}
