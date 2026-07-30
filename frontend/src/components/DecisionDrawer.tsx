import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import {
  companyLabel,
  formatAuditPayload,
  humanizeIdentifier,
  productLabel,
} from "../domainLabels";
import { formatSignedValue, formatValue } from "../format";
import { isAbortError, requestErrorMessage } from "../requestErrors";
import {
  clockTime,
  commandSummary,
  effectSummary,
  plural,
  shortId,
  stateChangeKey,
  stateChangeSummary,
  systemLabel,
  wakeLabel,
} from "../timelineFormatters";
import type {
  CommandStateChangeView,
  EconomicEffectView,
  JsonValue,
  SystemTimelineItemView,
  TimelineContextView,
  TimelineDetailView,
  TurnTimelineItemView,
} from "../types";
import { TimelineError, TimelineNotice } from "./TimelineFeedback";

type DetailLoader = (
  runId: string,
  entryId: string,
  signal?: AbortSignal,
) => Promise<TimelineDetailView>;

interface DecisionDrawerProps {
  readonly entryId: string;
  readonly loadDetail: DetailLoader;
  readonly onClose: () => void;
  readonly onSelectEntry: (entryId: string) => void;
  readonly runId: string;
}

export function DecisionDrawer({
  entryId,
  loadDetail,
  onClose,
  onSelectEntry,
  runId,
}: DecisionDrawerProps) {
  const [detail, setDetail] = useState<TimelineDetailView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const closeButton = useRef<HTMLButtonElement | null>(null);
  const dialog = useRef<HTMLElement | null>(null);
  const closeAction = useRef(onClose);

  useEffect(() => {
    closeAction.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const controller = new AbortController();
    setDetail(null);
    setError(null);
    closeButton.current?.focus();
    void loadDetail(runId, entryId, controller.signal)
      .then(setDetail)
      .catch((reason: unknown) => {
        if (!isAbortError(reason)) {
          setError(
            requestErrorMessage(reason, {
              fallback: "This timeline entry could not be loaded.",
            }),
          );
        }
      });
    return () => controller.abort();
  }, [entryId, loadDetail, runId]);

  useEffect(() => {
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    closeButton.current?.focus();

    function containFocus(event: KeyboardEvent): void {
      if (event.key === "Escape") {
        event.preventDefault();
        closeAction.current();
        return;
      }
      if (event.key !== "Tab" || dialog.current === null) {
        return;
      }
      const focusable = focusableElements(dialog.current);
      if (focusable.length === 0) {
        event.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (first === undefined || last === undefined) {
        return;
      }
      const active = document.activeElement;
      if (
        event.shiftKey &&
        (active === first || !dialog.current.contains(active))
      ) {
        event.preventDefault();
        last.focus();
      } else if (
        !event.shiftKey &&
        (active === last || !dialog.current.contains(active))
      ) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", containFocus);
    return () => {
      document.removeEventListener("keydown", containFocus);
      if (previousFocus?.isConnected) {
        previousFocus.focus();
      }
    };
  }, []);

  return (
    <div className="drawer-layer" role="presentation">
      <button
        aria-label="Close timeline detail"
        className="drawer-backdrop"
        onClick={onClose}
        type="button"
      />
      <aside
        aria-label="Timeline entry detail"
        aria-modal="true"
        className="decision-drawer"
        ref={dialog}
        role="dialog"
      >
        <header className="drawer-header">
          <div>
            <span className="eyebrow">AUDITABLE DECISION LOOP</span>
            <h2>{detail ? detailTitle(detail) : "Loading detail…"}</h2>
          </div>
          <button
            aria-label="Close"
            onClick={onClose}
            ref={closeButton}
            type="button"
          >
            ×
          </button>
        </header>
        {error ? (
          <TimelineError message={error} />
        ) : detail === null ? (
          <TimelineNotice label="Loading observation and trace provenance…" />
        ) : detail.entry.entryType === "turn" ? (
          <TurnDetail
            detail={detail}
            onSelectEntry={onSelectEntry}
            turn={detail.entry}
          />
        ) : (
          <SystemDetail detail={detail} step={detail.entry} />
        )}
      </aside>
    </div>
  );
}

function TurnDetail({
  detail,
  onSelectEntry,
  turn,
}: {
  readonly detail: TimelineDetailView;
  readonly onSelectEntry: (entryId: string) => void;
  readonly turn: TurnTimelineItemView;
}) {
  return (
    <div className="drawer-body">
      <DrawerSection number="1" title="Why the company acted">
        <ul className="detail-list">
          {turn.wakeSignals.map((signal, index) => {
            const sourceEntryId = signal.sourceEntryId;
            return (
              <li key={`${signal.reason}-${index}`}>
                <strong>{wakeLabel(signal.reason)}</strong>
                {sourceEntryId !== null && (
                  <button
                    onClick={() => onSelectEntry(sourceEntryId)}
                    type="button"
                  >
                    Open source {signal.sourceEntryType?.replace("_", " ")}{" "}
                    {shortId(sourceEntryId)}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      </DrawerSection>

      <DrawerSection number="2" title="What the agent observed">
        <ObservationDelta turn={turn} />
      </DrawerSection>

      <DrawerSection number="3" title="Command and engine outcome">
        <div className="command-outcome-detail">
          <div>
            <span>Atomic command</span>
            <strong>{commandSummary(turn.command)}</strong>
          </div>
          <div>
            <span>Engine result</span>
            <strong className={turn.accepted ? "value-up" : "value-down"}>
              {turn.accepted ? "Accepted" : "Rejected"}
            </strong>
            {turn.reason && <p>{turn.reason}</p>}
          </div>
        </div>
        {turn.effects.length > 0 && <EffectList effects={turn.effects} />}
        <StateChangeList changes={turn.stateChanges} />
        {turn.effects.length === 0 && turn.stateChanges.length === 0 && (
          <p className="inline-empty">No immediate economic change.</p>
        )}
        <p className="next-action">
          {turn.nextAvailableMinute === null
            ? "No continuation was scheduled."
            : `The company became available again at ${clockTime(turn.nextAvailableMinute)}.`}
        </p>
      </DrawerSection>

      <DrawerSection number="4" title="Agent trace provenance">
        <TraceProvenance context={detail.context} traces={detail.traces} turn={turn} />
      </DrawerSection>

      <RawAudit
        data={detail.turnRecord}
        label="Complete TurnRecord journal payload"
      />
    </div>
  );
}

function SystemDetail({
  detail,
  step,
}: {
  readonly detail: TimelineDetailView;
  readonly step: SystemTimelineItemView;
}) {
  return (
    <div className="drawer-body">
      <DrawerSection number="SYS" title={systemLabel(step.kind)}>
        <p>{step.summary}</p>
        <dl className="detail-metadata">
          <Meta label="Simulation time" value={clockTime(step.simMinute)} />
          <Meta
            label="State version"
            value={
              step.stateVersionBefore === null
                ? "Not available"
                : `v${step.stateVersionBefore} → v${step.stateVersionAfter}`
            }
          />
          <Meta
            label="Journal"
            value={
              step.journalSequence === null
                ? "Reconstructed view"
                : `#${step.journalSequence}`
            }
          />
          <Meta
            label="Companies affected"
            value={
              step.affectedCompanyIds.length > 0
                ? step.affectedCompanyIds
                    .map((companyId) => companyLabel(companyId))
                    .join(", ")
                : "No direct economic impact"
            }
          />
        </dl>
      </DrawerSection>
      <DrawerSection number="FX" title="Economic effects">
        <EffectList effects={step.effects} />
      </DrawerSection>
      {step.reconstructed && (
        <p className="reconstruction-note">
          This system milestone was deterministically reconstructed for a run
          created before the unified system journal was introduced.
        </p>
      )}
      <RawAudit
        data={detail.systemStepRecord}
        label="Complete SystemStepRecord journal payload"
      />
    </div>
  );
}

function ObservationDelta({ turn }: { readonly turn: TurnTimelineItemView }) {
  const delta = turn.observationDelta;
  return (
    <>
      <div className="delta-grid">
        <DeltaMetric
          after={formatValue(delta.cashAfter)}
          before={
            delta.cashBefore === null
              ? "First observation"
              : formatValue(delta.cashBefore)
          }
          change={
            delta.cashChange === null ? null : formatSignedValue(delta.cashChange)
          }
          label="Cash"
        />
        <DeltaMetric
          after={String(delta.openOrderCountAfter)}
          before={
            delta.openOrderCountBefore === null
              ? "First observation"
              : String(delta.openOrderCountBefore)
          }
          change={null}
          label="Open orders"
        />
        {delta.retailPriceAfter !== null && (
          <DeltaMetric
            after={formatValue(delta.retailPriceAfter)}
            before={
              delta.retailPriceBefore === null
                ? "Not set"
                : formatValue(delta.retailPriceBefore)
            }
            change={null}
            label="Retail price"
          />
        )}
      </div>
      <div className="inventory-deltas">
        {delta.inventory.map((inventory) => (
          <div key={inventory.product}>
            <span>{productLabel(inventory.product)}</span>
            <strong>
              {inventory.before === null
                ? "First observation"
                : formatValue(inventory.before)}
              {" → "}
              {formatValue(inventory.after)}
            </strong>
            {inventory.change !== null && (
              <small>{formatSignedValue(inventory.change)}</small>
            )}
          </div>
        ))}
      </div>
      <p className="observation-footnote">
        {turn.observation.visibleEventCount} visible economic{" "}
        {plural(turn.observation.visibleEventCount, "event")} at this turn.
      </p>
      {turn.observation.openOrders.length > 0 && (
        <div className="observed-facts">
          <h4>Open orders</h4>
          <ul className="effect-list">
            {turn.observation.openOrders.map((order) => (
              <li key={order.orderId}>
                <span>{order.side} order</span>
                <strong>
                  {formatValue(order.remainingQuantity)}{" "}
                  {productLabel(order.product)} at{" "}
                  {formatValue(order.limitPrice)} · {shortId(order.orderId)}
                </strong>
              </li>
            ))}
          </ul>
        </div>
      )}
      {turn.observation.visibleEvents.length > 0 && (
        <div className="observed-facts">
          <h4>Newly visible economic facts</h4>
          <EffectList effects={turn.observation.visibleEvents} />
        </div>
      )}
    </>
  );
}

function DeltaMetric({
  after,
  before,
  change,
  label,
}: {
  readonly after: string;
  readonly before: string;
  readonly change: string | null;
  readonly label: string;
}) {
  return (
    <div>
      <span>{label}</span>
      <small>{before}</small>
      <strong>{after}</strong>
      {change && <em>{change}</em>}
    </div>
  );
}

function TraceProvenance({
  context,
  traces,
  turn,
}: {
  readonly context: TimelineContextView;
  readonly traces: TimelineDetailView["traces"];
  readonly turn: TurnTimelineItemView;
}) {
  return (
    <div className="trace-detail">
      {context.isReplay && (
        <div className="source-lineage">
          <strong>This turn made no new model call.</strong>
          <span>
            Direct replay parent:{" "}
            <code>{context.sourceRunId ?? "Not recorded"}</code>
          </span>
          <span>
            Ultimate decision source run:{" "}
            <code>{turn.sourceRunId ?? context.traceRunId}</code>
          </span>
          <span>
            Ultimate source turn:{" "}
            <code>{turn.sourceTurnId ?? "Not recorded"}</code>
          </span>
          <span>
            Current replay usage: {formatValue(context.currentUsage.totalTokens)}{" "}
            tokens
          </span>
        </div>
      )}
      {traces.length === 0 ? (
        <p className="inline-empty">
          {context.mode === "baseline"
            ? "This decision came from a deterministic rule policy, so no model trace exists."
            : "No physical model-call trace is attached to this turn."}
        </p>
      ) : (
        traces.map((trace) => (
          <article className="agent-trace" key={trace.preview.invocationId}>
            <header>
              <div>
                <strong>
                  {trace.preview.provider} · {trace.preview.model}
                </strong>
                <span>
                  {trace.preview.isSourceTrace ? "Source trace" : "Current-run trace"}
                  {" · "}
                  {trace.preview.appliedToCommittedTurn
                    ? "Committed command"
                    : "Uncommitted physical attempt"}
                </span>
              </div>
              <span>{formatValue(trace.preview.usage.totalTokens)} tokens</span>
            </header>
            <dl className="detail-metadata trace-metadata">
              <Meta label="Trace run" value={trace.preview.traceRunId} />
              <Meta label="Invocation" value={trace.preview.invocationId} />
              <Meta
                label="Provider outcome"
                value={humanizeIdentifier(trace.preview.outcome)}
              />
              <Meta
                label="Runtime"
                value={`${formatValue(trace.preview.latencyMs)} ms · ${trace.preview.attempts} ${plural(trace.preview.attempts, "attempt")}`}
              />
            </dl>
            {trace.artifactStatus === "available" ? (
              <div className="trace-copy-grid">
                <TraceCopy
                  empty="No public reasoning summary is available."
                  label="Public reasoning summary"
                  value={trace.reasoningMarkdown}
                />
                <TraceCopy
                  empty="No final model output is available."
                  label="Final output"
                  value={trace.finalOutput}
                />
              </div>
            ) : (
              <p className="inline-empty">
                Trace artifact unavailable:{" "}
                {artifactUnavailableReasonLabel(
                  trace.artifactUnavailableReason,
                )}
                . Provider-call metadata remains auditable above.
              </p>
            )}
          </article>
        ))
      )}
    </div>
  );
}

function TraceCopy({
  empty,
  label,
  value,
}: {
  readonly empty: string;
  readonly label: string;
  readonly value: string | null;
}) {
  return (
    <section>
      <h4>{label}</h4>
      {value?.trim() ? <pre>{value}</pre> : <p>{empty}</p>}
    </section>
  );
}

function DrawerSection({
  children,
  number,
  title,
}: {
  readonly children: ReactNode;
  readonly number: string;
  readonly title: string;
}) {
  return (
    <section className="drawer-section">
      <header>
        <span>{number}</span>
        <h3>{title}</h3>
      </header>
      <div>{children}</div>
    </section>
  );
}

function EffectList({
  effects,
}: {
  readonly effects: readonly EconomicEffectView[];
}) {
  if (effects.length === 0) {
    return <p className="inline-empty">No economic effect was emitted.</p>;
  }
  return (
    <ul className="effect-list">
      {effects.map((effect, index) => (
        <li key={`${effect.kind}-${index}`}>
          <span>{humanizeIdentifier(effect.kind)}</span>
          <strong>{effectSummary(effect)}</strong>
        </li>
      ))}
    </ul>
  );
}

function StateChangeList({
  changes,
}: {
  readonly changes: readonly CommandStateChangeView[];
}) {
  if (changes.length === 0) {
    return null;
  }
  return (
    <ul className="effect-list state-change-list">
      {changes.map((change) => (
        <li key={stateChangeKey(change)}>
          <span>{humanizeIdentifier(change.changeType)}</span>
          <strong>{stateChangeSummary(change)}</strong>
        </li>
      ))}
    </ul>
  );
}

function RawAudit({
  data,
  label,
}: {
  readonly data: JsonValue | null;
  readonly label: string;
}) {
  return (
    <details className="raw-audit">
      <summary>{label}</summary>
      {data === null ? (
        <p>The immutable journal record is unavailable.</p>
      ) : (
        <>
          <p>
            Legacy display names are translated for this English UI; the stored
            journal remains unchanged.
          </p>
          <pre>{formatAuditPayload(data)}</pre>
        </>
      )}
    </details>
  );
}

function Meta({ label, value }: { readonly label: string; readonly value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd title={value}>{value}</dd>
    </div>
  );
}

function detailTitle(detail: TimelineDetailView): string {
  return detail.entry.entryType === "turn"
    ? `${detail.entry.companyName} · ${clockTime(detail.entry.simMinute)}`
    : `${systemLabel(detail.entry.kind)} · ${clockTime(detail.entry.simMinute)}`;
}

function artifactUnavailableReasonLabel(
  reason: TimelineDetailView["traces"][number]["artifactUnavailableReason"],
): string {
  switch (reason) {
    case "store_not_configured":
      return "the artifact store is not configured";
    case "provider_not_supported":
      return "this provider does not emit a readable artifact";
    case "identity_unavailable":
      return "the provider artifact identity was not recorded";
    case "not_found":
      return "the recorded artifact could not be found";
    case "read_error":
      return "the recorded artifact could not be read";
    default:
      return "no reason was recorded";
  }
}

function focusableElements(container: HTMLElement): readonly HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), summary, textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => element.getAttribute("aria-hidden") !== "true");
}
