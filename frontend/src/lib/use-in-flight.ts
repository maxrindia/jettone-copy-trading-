"use client";

import { useEffect, useRef, useState } from "react";
import { InFlightGuard } from "@/lib/in-flight-guard";

/** A per-component InFlightGuard that re-renders its component whenever a key becomes busy or idle. */
export function useInFlight(): InFlightGuard {
  const ref = useRef<InFlightGuard | null>(null);
  if (ref.current === null) ref.current = new InFlightGuard();
  const guard = ref.current;
  const [, bump] = useState(0);
  useEffect(() => guard.subscribe(() => bump((n) => n + 1)), [guard]);
  return guard;
}
