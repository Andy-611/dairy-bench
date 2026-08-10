import { useCallback, useEffect, useMemo, useState } from "react";

import type { DairyBenchApi } from "../shared/api/client";
import { isAbortError, requestErrorMessage } from "../shared/requestErrors";
import { isActiveRun } from "../shared/runStatus";
import type {
  EpisodeView,
  ReplaySourceView,
  RunJobView,
} from "../shared/api/types";

interface LocationSelection {
  readonly day: number | null;
  readonly runId: string;
}

export interface RunWorkspace {
  readonly activeJobs: readonly RunJobView[];
  readonly canLoadMoreRuns: boolean;
  readonly clearError: () => void;
  readonly episode: EpisodeView | null;
  readonly episodeError: string | null;
  readonly error: string | null;
  readonly isEpisodeLoading: boolean;
  readonly isReplaySourceLoading: boolean;
  readonly isRunHistoryLoading: boolean;
  readonly jobs: readonly RunJobView[];
  readonly loadMoreRuns: () => void;
  readonly replaySourceError: string | null;
  readonly replaySources: readonly ReplaySourceView[];
  readonly selectedDay: number | null;
  readonly selectedJob: RunJobView | null;
  readonly selectedReplaySourceId: string;
  readonly selectedRunId: string;
  readonly selectDay: (day: number) => void;
  readonly selectReplaySource: (runId: string) => void;
  readonly selectRun: (runId: string) => void;
  readonly timelineRevision: number;
  readonly trackJob: (job: RunJobView, select: boolean) => void;
}

const ACTIVE_RUN_POLL_INTERVAL_MS = 500;
const RUN_HISTORY_PAGE_SIZE = 100;
const REPLAY_SOURCE_REFRESH_INTERVAL_MS = 30_000;
const TIMELINE_REFRESH_INTERVAL_MS = 5_000;

export function useRunWorkspace(api: DairyBenchApi): RunWorkspace {
  const initialSelection = useMemo(readLocationSelection, []);
  const [jobs, setJobs] = useState<readonly RunJobView[]>([]);
  const [replaySources, setReplaySources] = useState<
    readonly ReplaySourceView[]
  >([]);
  const [selectedRunId, setSelectedRunId] = useState(initialSelection.runId);
  const [selectedDay, setSelectedDay] = useState<number | null>(
    initialSelection.day,
  );
  const [episode, setEpisode] = useState<EpisodeView | null>(null);
  const [episodeError, setEpisodeError] = useState<string | null>(null);
  const [jobListError, setJobListError] = useState<string | null>(null);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const [replaySourceError, setReplaySourceError] = useState<string | null>(null);
  const [isJobListLoading, setIsJobListLoading] = useState(true);
  const [isEpisodeLoading, setIsEpisodeLoading] = useState(false);
  const [isReplaySourceLoading, setIsReplaySourceLoading] = useState(true);
  const [canLoadMoreRuns, setCanLoadMoreRuns] = useState(false);
  const [runHistoryOffset, setRunHistoryOffset] = useState(0);
  const [replaySourceRevision, setReplaySourceRevision] = useState(0);
  const [timelineRevision, setTimelineRevision] = useState(0);

  const selectedJob =
    jobs.find((candidate) => candidate.runId === selectedRunId) ?? null;
  const activeJobs = jobs.filter((job) => isActiveRun(job.status));
  const activeRunKey = activeJobs.map((job) => job.runId).join("|");
  const selectedReplaySourceId = replaySources.some(
    (source) => source.runId === selectedRunId,
  )
    ? selectedRunId
    : (replaySources[0]?.runId ?? "");

  const clearError = useCallback(() => {
    setJobListError(null);
    setReplaySourceError(null);
    setSelectionError(null);
  }, []);

  const selectRun = useCallback(
    (runId: string, historyMode: "push" | "replace" = "push") => {
      const job = jobs.find((candidate) => candidate.runId === runId);
      const day = job ? latestTimelineDay(job) : null;
      setSelectedRunId(runId);
      setSelectedDay(day);
      writeLocation(runId, day ?? 1, historyMode);
    },
    [jobs],
  );

  const selectReplaySource = useCallback(
    (runId: string) => {
      if (runId && runId !== selectedRunId) {
        selectRun(runId);
      }
    },
    [selectRun, selectedRunId],
  );

  const selectDay = useCallback(
    (day: number) => {
      if (!selectedJob) {
        return;
      }
      const nextDay = clampDay(day, selectedJob);
      setSelectedDay(nextDay);
      writeLocation(selectedJob.runId, nextDay, "push");
    },
    [selectedJob],
  );

  const recordJobs = useCallback((incoming: readonly RunJobView[]) => {
    setJobs((current) => mergeJobs(current, incoming));
    const completed = incoming
      .filter((job) => job.status === "completed")
      .map((job) => ({
        runId: job.runId,
        submittedAt: job.submittedAt,
        benchmarkEligible: job.quality?.benchmarkEligible ?? false,
      }));
    if (completed.length > 0) {
      setReplaySources((current) => mergeReplaySources(current, completed));
    }
  }, []);

  const trackJob = useCallback((job: RunJobView, select: boolean) => {
    recordJobs([job]);
    if (select) {
      const day = latestTimelineDay(job);
      setSelectedRunId(job.runId);
      setSelectedDay(day);
      writeLocation(job.runId, day, "push");
    }
  }, [recordJobs]);

  useEffect(() => {
    const controller = new AbortController();
    setIsJobListLoading(true);
    setJobListError(null);
    void api
      .runJobs(RUN_HISTORY_PAGE_SIZE, 0, controller.signal)
      .then((nextJobs) => {
        recordJobs(nextJobs);
        setRunHistoryOffset(nextJobs.length);
        setCanLoadMoreRuns(nextJobs.length === RUN_HISTORY_PAGE_SIZE);
      })
      .catch((reason: unknown) => {
        if (!isAbortError(reason)) {
          setJobListError(
            requestErrorMessage(reason, {
              fallback: "The current run could not be restored.",
            }),
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsJobListLoading(false);
        }
      });
    return () => controller.abort();
  }, [api, recordJobs]);

  const loadMoreRuns = useCallback(() => {
    if (isJobListLoading || !canLoadMoreRuns) {
      return;
    }
    setIsJobListLoading(true);
    setJobListError(null);
    void api
      .runJobs(RUN_HISTORY_PAGE_SIZE, runHistoryOffset)
      .then((nextJobs) => {
        recordJobs(nextJobs);
        setRunHistoryOffset((offset) => offset + nextJobs.length);
        setCanLoadMoreRuns(nextJobs.length === RUN_HISTORY_PAGE_SIZE);
      })
      .catch((reason: unknown) => {
        if (!isAbortError(reason)) {
          setJobListError(
            requestErrorMessage(reason, {
              fallback: "More run history could not be loaded.",
            }),
          );
        }
      })
      .finally(() => setIsJobListLoading(false));
  }, [api, canLoadMoreRuns, isJobListLoading, recordJobs, runHistoryOffset]);

  useEffect(() => {
    const controller = new AbortController();
    setReplaySourceError(null);
    void api
      .replaySources(controller.signal)
      .then(setReplaySources)
      .catch((reason: unknown) => {
        if (!isAbortError(reason)) {
          setReplaySourceError(
            requestErrorMessage(reason, {
              fallback: "Completed replay sources could not be loaded.",
            }),
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsReplaySourceLoading(false);
        }
      });
    return () => controller.abort();
  }, [api, replaySourceRevision]);

  useEffect(() => {
    function refreshReplaySources(): void {
      setReplaySourceRevision((revision) => revision + 1);
    }
    const interval = window.setInterval(
      refreshReplaySources,
      REPLAY_SOURCE_REFRESH_INTERVAL_MS,
    );
    window.addEventListener("focus", refreshReplaySources);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener("focus", refreshReplaySources);
    };
  }, []);

  useEffect(() => {
    if (selectedRunId || isJobListLoading) {
      return;
    }
    const runId = activeJobs[0]?.runId ?? jobs[0]?.runId;
    if (runId) {
      selectRun(runId, "replace");
    }
  }, [
    isJobListLoading,
    activeJobs[0]?.runId,
    jobs,
    selectRun,
    selectedRunId,
  ]);

  useEffect(() => {
    function restoreLocation(): void {
      const selection = readLocationSelection();
      setSelectedRunId(selection.runId);
      setSelectedDay(selection.day);
    }

    window.addEventListener("popstate", restoreLocation);
    return () => window.removeEventListener("popstate", restoreLocation);
  }, []);

  useEffect(() => {
    if (!selectedRunId || selectedJob || isJobListLoading) {
      setSelectionError(null);
      return;
    }

    const controller = new AbortController();
    setSelectionError(null);
    void api
      .runJob(selectedRunId, controller.signal)
      .then((job) => recordJobs([job]))
      .catch((reason: unknown) => {
        if (!isAbortError(reason)) {
          setSelectionError(
            requestErrorMessage(reason, {
              fallback: `Run ${selectedRunId} could not be loaded.`,
            }),
          );
        }
      });
    return () => controller.abort();
  }, [api, isJobListLoading, recordJobs, selectedJob, selectedRunId]);

  useEffect(() => {
    if (!activeRunKey) {
      return;
    }

    const controller = new AbortController();
    const runIds = activeRunKey.split("|");
    let timer: number | null = null;

    async function poll(): Promise<void> {
      try {
        const nextJobs = await Promise.all(
          runIds.map((runId) => api.runJob(runId, controller.signal)),
        );
        setJobListError(null);
        recordJobs(nextJobs);
        if (nextJobs.some((job) => isActiveRun(job.status))) {
          timer = window.setTimeout(poll, ACTIVE_RUN_POLL_INTERVAL_MS);
        }
      } catch (reason: unknown) {
        if (isAbortError(reason)) {
          return;
        }
        setJobListError(
          requestErrorMessage(reason, {
            fallback: "Active runs could not be refreshed.",
          }),
        );
        timer = window.setTimeout(poll, ACTIVE_RUN_POLL_INTERVAL_MS * 4);
      }
    }

    timer = window.setTimeout(poll, ACTIVE_RUN_POLL_INTERVAL_MS);
    return () => {
      controller.abort();
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    };
  }, [
    activeRunKey,
    api,
    recordJobs,
  ]);

  useEffect(() => {
    if (!selectedJob) {
      return;
    }
    const clampedDay = clampDay(
      selectedDay ?? latestTimelineDay(selectedJob),
      selectedJob,
    );
    if (clampedDay !== selectedDay) {
      setSelectedDay(clampedDay);
      writeLocation(selectedJob.runId, clampedDay, "replace");
    }
  }, [selectedDay, selectedJob]);

  useEffect(() => {
    if (!selectedJob || !isActiveRun(selectedJob.status)) {
      return;
    }
    const interval = window.setInterval(
      () => setTimelineRevision((revision) => revision + 1),
      TIMELINE_REFRESH_INTERVAL_MS,
    );
    return () => window.clearInterval(interval);
  }, [selectedJob?.runId, selectedJob?.status]);

  useEffect(() => {
    setEpisode(null);
    setEpisodeError(null);
    if (selectedJob?.status !== "completed") {
      setIsEpisodeLoading(false);
      return;
    }

    const controller = new AbortController();
    setIsEpisodeLoading(true);
    void api
      .episode(selectedJob.runId, controller.signal)
      .then(setEpisode)
      .catch((reason: unknown) => {
        if (!isAbortError(reason)) {
          setEpisodeError(
            requestErrorMessage(reason, {
              fallback: "The completed run result could not be loaded.",
            }),
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsEpisodeLoading(false);
        }
      });
    return () => controller.abort();
  }, [api, selectedJob?.runId, selectedJob?.status]);

  return {
    activeJobs,
    canLoadMoreRuns,
    clearError,
    episode,
    episodeError,
    error: jobListError ?? selectionError,
    isEpisodeLoading,
    isReplaySourceLoading,
    isRunHistoryLoading: isJobListLoading,
    jobs,
    loadMoreRuns,
    replaySourceError,
    replaySources,
    selectedDay,
    selectedJob,
    selectedReplaySourceId,
    selectedRunId,
    selectDay,
    selectReplaySource,
    selectRun,
    timelineRevision,
    trackJob,
  };
}

function mergeJobs(
  current: readonly RunJobView[],
  incoming: readonly RunJobView[],
): readonly RunJobView[] {
  const jobs = new Map(current.map((job) => [job.runId, job]));
  let changed = false;
  incoming.forEach((job) => {
    const existing = jobs.get(job.runId);
    if (!existing || job.revision > existing.revision) {
      jobs.set(job.runId, job);
      changed = true;
    }
  });
  return changed ? [...jobs.values()].sort(byNewestSubmission) : current;
}

function mergeReplaySources(
  current: readonly ReplaySourceView[],
  incoming: readonly ReplaySourceView[],
): readonly ReplaySourceView[] {
  return [
    ...new Map(
      [...current, ...incoming].map((source) => [source.runId, source]),
    ).values(),
  ].sort(byNewestSubmission);
}

function byNewestSubmission(
  left: { readonly submittedAt: string },
  right: { readonly submittedAt: string },
): number {
  return Date.parse(right.submittedAt) - Date.parse(left.submittedAt);
}

function latestTimelineDay(job: RunJobView): number {
  return job.status === "completed"
    ? job.totalDays
    : Math.min(job.totalDays, job.currentDay + 1);
}

function clampDay(day: number, job: RunJobView): number {
  return Math.max(1, Math.min(latestTimelineDay(job), Math.trunc(day)));
}

function readLocationSelection(): LocationSelection {
  const query = new URLSearchParams(window.location.search);
  const rawDay = Number(query.get("day"));
  return {
    runId: query.get("run")?.trim() ?? "",
    day: Number.isInteger(rawDay) && rawDay > 0 ? rawDay : null,
  };
}

function writeLocation(
  runId: string,
  day: number,
  mode: "push" | "replace",
): void {
  const url = new URL(window.location.href);
  url.searchParams.set("run", runId);
  url.searchParams.set("day", String(day));
  window.history[`${mode}State`](null, "", url);
}
