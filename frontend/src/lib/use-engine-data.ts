"use client";

import { useCallback, useEffect, useState } from "react";
import { isUnauthenticated, redirectToSignIn } from "@/lib/session-guard";

type EngineDataState<T> = {
  data: T | null;
  error: string | null;
  isLoading: boolean;
  refresh: () => void;
};

export function useEngineData<T>(path: string | null, pollMs?: number): EngineDataState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(path !== null);
  const [tick, setTick] = useState(0);

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!path) {
      setIsLoading(false);
      return;
    }

    let cancelled = false;
    const load = async () => {
      try {
        const resp = await fetch(`/api/v1${path}`);
        if (isUnauthenticated(resp)) {
          // Session ended (e.g. logged out in another tab / restored page): leave, do not parse HTML.
          redirectToSignIn();
          return;
        }
        const contentType = resp.headers.get("content-type") ?? "";
        if (!contentType.includes("application/json")) {
          if (!cancelled) setError(`Unexpected response from the server (${resp.status}).`);
          return;
        }
        const body = (await resp.json()) as unknown;
        if (cancelled) return;

        if (!resp.ok) {
          setError(extractError(body) ?? `Request failed (${resp.status})`);
          setData(null);
        } else {
          setData(body as T);
          setError(null);
        }
      } catch {
        if (!cancelled) setError("Network error while contacting the trading engine.");
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };

    setIsLoading(true);
    void load();

    let interval: ReturnType<typeof setInterval> | undefined;
    if (pollMs && pollMs > 0) {
      interval = setInterval(() => void load(), pollMs);
    }
    return () => {
      cancelled = true;
      if (interval) clearInterval(interval);
    };
  }, [path, tick, pollMs]);

  return { data, error, isLoading, refresh };
}

export function extractError(body: unknown): string | null {
  if (
    body !== null &&
    typeof body === "object" &&
    "error" in body &&
    body.error !== null &&
    typeof body.error === "object" &&
    "message" in body.error &&
    typeof (body.error as { message: unknown }).message === "string"
  ) {
    return (body.error as { message: string }).message;
  }
  if (
    body !== null &&
    typeof body === "object" &&
    "detail" in body &&
    typeof (body as { detail: unknown }).detail === "string"
  ) {
    return (body as { detail: string }).detail;
  }
  return null;
}
