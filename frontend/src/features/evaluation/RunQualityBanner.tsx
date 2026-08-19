import type { EpisodeQualityView, ProtocolIssueKind } from "../../shared/api/types";

interface RunQualityBannerProps {
  readonly completedWeeks: number;
  readonly provisional: boolean;
  readonly quality: EpisodeQualityView;
  readonly totalWeeks: number;
}

const ISSUE_LABELS: Readonly<Record<ProtocolIssueKind, string>> = {
  context_too_large: "context too large",
  invalid_arguments: "invalid tool arguments",
  invalid_response: "invalid provider response",
  missing_tool_call: "missing tool call",
  multiple_tool_calls: "multiple tool calls",
  unauthorized_decision_tool: "unauthorized decision tool",
};

export function RunQualityBanner({
  completedWeeks,
  provisional,
  quality,
  totalWeeks,
}: RunQualityBannerProps) {
  if (!provisional && quality.benchmarkEligible) {
    return null;
  }
  const issues = quality.issues
    .map((issue) => `${issue.count} ${ISSUE_LABELS[issue.kind]}`)
    .join(" · ");
  return (
    <section
      className={`quality-banner${quality.benchmarkEligible ? " provisional" : ""}`}
      role="status"
    >
      <span aria-hidden="true">{quality.benchmarkEligible ? "i" : "!"}</span>
      <div>
        <strong>
          {provisional
            ? `Provisional result through week ${completedWeeks} of ${totalWeeks}`
            : `Run completed, but ${quality.invalidTurnCount} of ${quality.totalTurnCount} turns violated the model-decision protocol`}
        </strong>
        <p>
          {provisional
            ? "It refreshes after each weekly settlement and remains outside formal rankings until the full horizon completes."
            : "The economic result is preserved for diagnosis but excluded from benchmark rankings."}
          {!quality.benchmarkEligible && ` Protocol issues so far: ${issues}.`}
        </p>
      </div>
    </section>
  );
}
