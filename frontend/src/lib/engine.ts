import "server-only";

const ENGINE_URL = process.env.TRADING_ENGINE_URL ?? "http://fastapi:8000";
const INTERNAL_TOKEN = process.env.INTERNAL_API_TOKEN ?? "";

export type EngineResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; error: string };

export async function engineFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<EngineResult<T>> {
  if (!INTERNAL_TOKEN) {
    return { ok: false, status: 503, error: "Internal API token is not configured." };
  }

  const url = `${ENGINE_URL}/api/v1${path}`;
  try {
    const resp = await fetch(url, {
      ...init,
      headers: {
        "X-Internal-Token": INTERNAL_TOKEN,
        "Content-Type": "application/json",
        ...(init.headers ?? {}),
      },
      cache: "no-store",
    });

    const text = await resp.text();
    const body = text ? (JSON.parse(text) as unknown) : null;

    if (!resp.ok) {
      let detail = `Engine returned ${resp.status}`;
      if (body !== null && typeof body === "object" && "detail" in body) {
        const d = (body as { detail: unknown }).detail;
        if (typeof d === "string") {
          detail = d;
        } else if (Array.isArray(d)) {
          const parts = d
            .map((item) =>
              item !== null && typeof item === "object" && "msg" in item
                ? String((item as { msg: unknown }).msg)
                : JSON.stringify(item),
            )
            .join("; ");
          detail = `Validation failed: ${parts}`;
        }
      }
      return { ok: false, status: resp.status, error: detail };
    }

    return { ok: true, data: body as T };
  } catch (err) {
    return {
      ok: false,
      status: 502,
      error: err instanceof Error ? err.message : "Trading engine unreachable.",
    };
  }
}
