import { NextRequest, NextResponse } from "next/server";
import { engineFetch } from "@/lib/engine";

type RouteContext = { params: { path: string[] } };

async function proxy(request: NextRequest, context: RouteContext) {
  const { path } = context.params;
  const search = request.nextUrl.search;
  const targetPath = `/${path.join("/")}${search}`;

  let body: string | undefined;
  if (request.method !== "GET" && request.method !== "HEAD") {
    body = await request.text();
  }

  // engineFetch always sets Content-Type: application/json for us; do not
  // forward the browser's header or duplicate values confuse FastAPI.
  const result = await engineFetch<unknown>(targetPath, {
    method: request.method,
    body,
  });

  if (!result.ok) {
    return NextResponse.json(
      { success: false, error: { code: "ENGINE_ERROR", message: result.error } },
      { status: result.status },
    );
  }

  return NextResponse.json(result.data, { status: 200 });
}

export async function GET(request: NextRequest, ctx: RouteContext) {
  return proxy(request, ctx);
}
export async function POST(request: NextRequest, ctx: RouteContext) {
  return proxy(request, ctx);
}
export async function PUT(request: NextRequest, ctx: RouteContext) {
  return proxy(request, ctx);
}
export async function PATCH(request: NextRequest, ctx: RouteContext) {
  return proxy(request, ctx);
}
export async function DELETE(request: NextRequest, ctx: RouteContext) {
  return proxy(request, ctx);
}
