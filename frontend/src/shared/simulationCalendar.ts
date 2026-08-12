import type { SimDayView } from "./api/types";

export const DAYS_PER_WEEK = 7;
export const WEEKDAY_NAMES = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
] as const;

export function simulationWeek(day: SimDayView | number): number {
  return Math.floor((absoluteDay(day) - 1) / DAYS_PER_WEEK) + 1;
}

export function weekdayName(day: SimDayView | number): string {
  return WEEKDAY_NAMES[(absoluteDay(day) - 1) % DAYS_PER_WEEK]!;
}

export function simulationDayLabel(day: SimDayView | number): string {
  const value = absoluteDay(day);
  return `Week ${simulationWeek(value)} · ${weekdayName(value)} · Day ${value}`;
}

export function completedWeeks(absoluteDayCount: number): number {
  return Math.floor(absoluteDayCount / DAYS_PER_WEEK);
}

function absoluteDay(day: SimDayView | number): number {
  return typeof day === "number" ? day : day.absoluteDay;
}
