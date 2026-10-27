"use client";

import { useEngineData } from "@/lib/use-engine-data";
import {
  brokerLabel,
  masterDetectionBlocker,
  matrixRows,
  roleSummary,
  type BrokerMatrixData,
} from "@/lib/broker-matrix";

/**
 * Master -> Child broker compatibility, as the engine reports it right now. Every pair other than a supported one is shown as
 * blocked with the engine's reason. Read-only: it enables nothing and the engine enforces every gate itself.
 */
export default function BrokerMatrixPanel() {
  const { data, error, isLoading } = useEngineData<{ data?: BrokerMatrixData }>("/brokers/matrix");
  const matrix = data?.data ?? null;
  const rows = matrixRows(matrix);

  return (
    <details data-testid="broker-matrix" className="bg-white border border-[#E5E7EB] rounded-[10px] shadow-[0_2px_8px_rgba(0,0,0,0.04)]">
      <summary className="cursor-pointer select-none px-4 py-3 text-sm font-bold text-[#111111]">Broker compatibility</summary>
      <div className="px-4 pb-4 space-y-4">
        {isLoading && <p className="text-xs text-[#6B7280]">Loading broker compatibility...</p>}
        {error && !isLoading && (
          <p role="alert" className="text-xs font-semibold text-red-700">
            {error} No pair is shown as supported.
          </p>
        )}
        {rows.length > 0 && matrix && (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-xs border-collapse">
                <caption className="sr-only">Master broker rows by Child broker columns</caption>
                <thead>
                  <tr>
                    <th scope="col" className="text-left font-semibold text-[#6B7280] py-2 pr-3">Master / Child</th>
                    {rows.map((r) => (
                      <th key={r.master} scope="col" className="text-left font-semibold text-[#111111] py-2 px-3">{brokerLabel(r.master)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.master} className="border-t border-[#E5E7EB]">
                      <th scope="row" className="text-left font-semibold text-[#111111] py-2 pr-3">{brokerLabel(r.master)}</th>
                      {r.cells.map((c) => (
                        <td key={c.child} className="py-2 px-3" title={c.reason} data-pair={`${r.master}>${c.child}`} data-supported={c.supported ? "yes" : "no"}>
                          <span
                            className={`inline-block text-[11px] font-semibold px-2 py-0.5 rounded border ${
                              c.supported ? "bg-green-50 border-green-200 text-green-800" : "bg-gray-100 border-gray-200 text-gray-600"
                            }`}
                          >
                            {c.supported ? "Supported" : "Blocked"}
                          </span>
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <ul className="space-y-2">
              {matrix.brokers.map((b) => {
                const blocker = masterDetectionBlocker(b);
                return (
                  <li key={b.id} className="text-[11px] leading-4 text-[#4B5563]" data-broker={b.id}>
                    <span className="font-semibold text-[#111111]">{brokerLabel(b.id)}</span>: Master {roleSummary(b.master)}, Child {roleSummary(b.child)}.
                    {blocker && <span className="block text-[#6B7280]">Master detection not verified: {blocker}</span>}
                  </li>
                );
              })}
            </ul>
            <p className="text-[11px] leading-4 text-[#6B7280]">{matrix.note}</p>
          </>
        )}
      </div>
    </details>
  );
}
