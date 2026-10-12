"use client";

import { useEffect } from "react";

export default function AppError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("[AppError boundary]", error);
  }, [error]);

  return (
    <div className="min-h-[60dvh] bg-[#F7F7F7] flex items-center justify-center p-6">
      <div className="w-full max-w-lg bg-white border border-red-200 rounded-[10px] shadow-sm p-8 space-y-4">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-[10px] bg-red-50 flex items-center justify-center shrink-0">
            <svg className="w-5 h-5 text-red-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
            </svg>
          </div>
          <div>
            <h1 className="text-sm font-bold text-[#111111]">Something went wrong</h1>
            <p className="text-xs text-gray-500 mt-0.5">A client-side error occurred in this page</p>
          </div>
        </div>
        <div className="bg-red-50 border border-red-100 rounded-[10px] p-4">
          <p className="text-xs font-semibold text-red-700 mb-1">Error: {error.name}</p>
          <p className="text-xs text-red-600 font-mono break-all leading-relaxed">{error.message}</p>
          {error.digest && <p className="text-[10px] text-red-400 mt-2">Digest: {error.digest}</p>}
        </div>
        {error.stack && (
          <details>
            <summary className="text-xs text-gray-500 cursor-pointer hover:text-gray-700">Stack trace</summary>
            <pre className="mt-2 text-[10px] text-gray-500 font-mono overflow-x-auto whitespace-pre-wrap bg-gray-50 border border-gray-100 rounded-lg p-3">{error.stack}</pre>
          </details>
        )}
        <div className="flex gap-3 pt-2">
          <button onClick={reset} className="flex-1 h-9 rounded-lg bg-jettone-orange hover:bg-jettone-orange-hover text-white text-xs font-semibold transition-colors">Try again</button>
          <button onClick={() => (window.location.href = "/")} className="flex-1 h-9 rounded-lg border border-gray-200 hover:bg-gray-50 text-gray-700 text-xs font-semibold transition-colors">Dashboard</button>
        </div>
      </div>
    </div>
  );
}
