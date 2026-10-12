"use client";

import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { useEngineData } from "@/lib/use-engine-data";
import { buildAccountOrdersApiPath } from "@/lib/account-history";
import { ErrorPanel, LoadingPanel } from "@/components/ui/DataStates";
import OrdersTable, { ORDER_STATUS_FILTERS, type OrdersTableAccountInfo, type UnifiedOrder } from "@/components/orders/OrdersTable";

interface AccountDetail extends OrdersTableAccountInfo {
  id: string;
  alice_user_id: string;
}

/**
 * Standalone order history for one Demat account - the "View" (eye) action
 * on Demat Accounts / Group Strategy child cards. Deliberately its own page
 * under /demat, not a filtered view of Order Manager (/orders), so it lives
 * under the Demat Accounts section and has its own dedicated backend route
 * (GET /accounts/{account_id}/orders) rather than reusing the cross-account
 * /trades/orders listing.
 */
export default function AccountOrderHistoryPage() {
  const params = useParams();
  const router = useRouter();
  const accountId = Array.isArray(params?.accountId) ? params.accountId[0] : (params?.accountId as string);

  const [filter, setFilter] = useState<(typeof ORDER_STATUS_FILTERS)[number]>("ALL");

  const { data: account, error: accountError } = useEngineData<AccountDetail>(
    accountId ? `/accounts/${accountId}` : null,
  );
  const { data: orders, error: ordersError, isLoading: ordersLoading, refresh } = useEngineData<UnifiedOrder[]>(
    accountId ? buildAccountOrdersApiPath(accountId) : null,
    5000, // auto refresh every 5s
  );

  const accountMap = new Map<string, OrdersTableAccountInfo>(account ? [[account.id, account]] : []);

  const filteredOrders = (orders ?? []).filter((o) => {
    if (filter === "ALL") return true;
    return o.status.toUpperCase() === filter;
  });

  return (
    <div className="space-y-6 max-w-page mx-auto pb-16">
      {/* ── Top Bar: Back Button, Account Name, Client ID ── */}
      <div className="bg-white text-[#111111] p-4 sm:p-5 rounded-[10px] border border-[#E5E7EB] shadow-sm flex flex-wrap items-center gap-4">
        <button
          onClick={() => router.push("/demat")}
          className="w-8 h-8 rounded-lg bg-[#111111] hover:bg-[#1F2937] text-white flex items-center justify-center transition-transform active:scale-95 shadow-sm shrink-0"
          title="Back to Demat Accounts"
        >
          <ArrowLeft className="w-4 h-4 stroke-[2.5]" />
        </button>

        <div className="min-w-0">
          <h1 className="text-base sm:text-lg font-extrabold text-[#111111] tracking-tight truncate">
            {account ? `${account.display_name} — Order History` : "Order History"}
          </h1>
          {account && (
            <p className="text-[11px] text-[#6B7280] mt-0.5">
              Account-specific order history for{" "}
              <span className="font-mono font-bold text-[#111111]">{account.alice_user_id}</span>
            </p>
          )}
        </div>
      </div>

      {accountError ? (
        <ErrorPanel message={accountError} onRetry={() => router.push("/demat")} />
      ) : (
        <>
          <div className="flex items-center justify-between flex-wrap gap-3">
            <div className="flex gap-1 bg-[#F3F4F6] p-1 rounded-[10px] border border-[#E5E7EB] flex-wrap">
              {ORDER_STATUS_FILTERS.map((f) => (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={`px-3.5 py-1.5 rounded-lg text-xs font-bold transition-all ${
                    filter === f
                      ? "bg-white text-[#111111] shadow-sm"
                      : "text-[#6B7280] hover:text-[#111111]"
                  }`}
                >
                  {f}
                </button>
              ))}
            </div>
            <span className="text-xs text-[#6B7280] font-bold tabular-nums">
              {filteredOrders.length} order{filteredOrders.length === 1 ? "" : "s"}
            </span>
          </div>

          {ordersLoading && <LoadingPanel label="Loading order records..." />}
          {ordersError && <ErrorPanel message={ordersError} onRetry={refresh} />}

          {!ordersLoading && !ordersError && <OrdersTable orders={filteredOrders} accountMap={accountMap} />}
        </>
      )}
    </div>
  );
}
