import type { EpisodeQualityView, ProtocolIssueKind } from "../../shared/api/types";

interface RunQualityBannerProps {
  readonly quality: EpisodeQualityView;
}

const ISSUE_LABELS: Readonly<Record<ProtocolIssueKind, string>> = {
  context_too_large: "context too large",
  invalid_arguments: "invalid tool arguments",
  invalid_response: "invalid provider response",
  missing_tool_call: "missing tool call",
  multiple_tool_calls: "multiple tool calls",
  unauthorized_decision_tool: "unauthorized decision tool",
};

export function RunQualityBanner({ quality }: RunQualityBannerProps) {
  if (quality.benchmarkEligible) {
    return null;
  }
  const issues = quality.issues
    .map((issue) => `${issue.count} ${ISSUE_LABELS[issue.kind]}`)
    .join(" · ");
  return (
    <section className="quality-banner" role="status">
      <span aria-hidden="true">!</span>
      <div>
        <strong>
          Run completed, but {quality.invalidTurnCount} of {quality.totalTurnCount}{" "}
          turns violated the model-decision protocol
        </strong>
        <p>
          The economic result is preserved for diagnosis but excluded from benchmark
          rankings. {issues}.
        </p>
      </div>
    </section>
  );
}
