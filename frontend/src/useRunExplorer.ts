import { useCallback, useEffect, useMemo, useState } from "react";

import type { DairyBenchApi } from "./api";
import { isAbortError, requestErrorMessage } from "./requestErrors";
import type { EpisodeView, RunJobView } from "./types";

interface LocationSelection {
  readonly day: number | null;
  readonly runId: string;
}

export interface RunExplorer {
  readonly episode: EpisodeView | null;
  readonly episodeError: string | null;
  readonly historyError: string | null;
  readonly isEpisodeLoading: boolean;
  readonly isHistoryLoading: boolean;
  readonly jobs: readonly RunJobView[];
  readonly refresh: () => void;
  readonly selectedDay: number | null;
  readonly selectedJob: RunJobView | null;
  readonly selectedRunId: string;
  readonly selectionError: string | null;
  readonly selectDay: (day: number) => void;
  readonly selectRun: (runId: string) => void;
  readonly timelineRevision: number;
  readonly trackJob: (job: RunJobView, select: boolean) => void;
}

export function useRunExplorer(api: DairyBenchApi): RunExplorer {
  const initialSelection = useMemo(readLocationSelection, []);
  const [jobs, setJobs] = useState<readonly RunJobView[]>([]);
  const [selectedRunId, setSelectedRunId] = useState(initialSelection.runId);
  const [selectedDay, setSelectedDay] = useState<number | null>(
    initialSelection.day,
  );
  const [episode, setEpisode] = useState<EpisodeView | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const [episodeError, setEpisodeError] = useState<string | null>(null);
  const [isHistoryLoading, setIsHistoryLoading] = useState(true);
  const [isEpisodeLoading, setIsEpisodeLoading] = useState(false);
  const [refreshSequence, setRefreshSequence] = useState(0);

  const selectedJob =
    jobs.find((candidate) => candidate.runId === selectedRunId) ?? null;

  useEffect(() => {
    const controller = new AbortController();
    setIsHistoryLoading(true);
    setHistoryError(null);
    void api
      .runJobs(100, controller.signal)
      .then((nextJobs) => setJobs((current) => mergeJobs(current, nextJobs)))
      .catch((reason: unknown) => {
        if (!isAbortError(reason)) {
          setHistoryError(
            requestErrorMessage(reason, {
              fallback: "Run history could not be loaded.",
            }),
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsHistoryLoading(false);
        }
      });
    return () => controller.abort();
  }, [api, refreshSequence]);

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
    if (!selectedRunId || selectedJob || isHistoryLoading) {
      setSelectionError(null);
      return;
    }

    const controller = new AbortController();
    setSelectionError(null);
    void api
      .runJob(selectedRunId, controller.signal)
      .then((job) => setJobs((current) => mergeJobs(current, [job])))
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
  }, [api, isHistoryLoading, selectedJob, selectedRunId]);

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

  const refresh = useCallback(
    () => setRefreshSequence((sequence) => sequence + 1),
    [],
  );

  const selectRun = useCallback(
    (runId: string) => {
      const job = jobs.find((candidate) => candidate.runId === runId);
      const day = job ? latestTimelineDay(job) : 1;
      setSelectedRunId(runId);
      setSelectedDay(day);
      writeLocation(runId, day, "push");
    },
    [jobs],
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

  const trackJob = useCallback((job: RunJobView, select: boolean) => {
    setJobs((current) => mergeJobs(current, [job]));
    if (select) {
      const day = latestTimelineDay(job);
      setSelectedRunId(job.runId);
      setSelectedDay(day);
      writeLocation(job.runId, day, "push");
    }
  }, []);

  return {
    episode,
    episodeError,
    historyError,
    isEpisodeLoading,
    isHistoryLoading,
    jobs,
    refresh,
    selectedDay,
    selectedJob,
    selectedRunId,
    selectionError,
    selectDay,
    selectRun,
    timelineRevision: refreshSequence,
    trackJob,
  };
}

function mergeJobs(
  current: readonly RunJobView[],
  incoming: readonly RunJobView[],
): readonly RunJobView[] {
  const byId = new Map(current.map((job) => [job.runId, job]));
  incoming.forEach((job) => {
    const existing = byId.get(job.runId);
    if (!existing || job.revision > existing.revision) {
      byId.set(job.runId, job);
    }
  });
  return [...byId.values()].sort(
    (left, right) => Date.parse(right.submittedAt) - Date.parse(left.submittedAt),
  );
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
