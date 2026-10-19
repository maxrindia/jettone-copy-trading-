/**
 * At most one running task per key (here: one Re-login per account).
 *
 * `run` marks the key busy SYNCHRONOUSLY, before the task's first await, so two clicks in the same tick - or a
 * click queued while a confirm dialog was open - can never both start the task. The key is always released
 * afterwards, whether the task succeeds, fails or throws. A second `run` while busy does nothing and returns false.
 * `subscribe` lets a UI re-render when the busy set changes (see use-in-flight.ts).
 */
export class InFlightGuard {
  private readonly active = new Set<string>();
  private readonly listeners = new Set<() => void>();

  isBusy(key: string): boolean {
    return this.active.has(key);
  }

  /** Runs `task` unless `key` is already running. Resolves true if it ran (and finished), false if it was skipped. */
  async run(key: string, task: () => Promise<void>): Promise<boolean> {
    if (this.active.has(key)) return false;
    this.active.add(key);
    this.notify();
    try {
      await task();
      return true;
    } finally {
      this.active.delete(key);
      this.notify();
    }
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private notify(): void {
    for (const listener of Array.from(this.listeners)) listener();
  }
}
