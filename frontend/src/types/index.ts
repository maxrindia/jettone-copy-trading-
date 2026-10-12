export type AccountType = "MASTER" | "FOLLOWER";

export type OrderStatus =
  | "PENDING"
  | "SUBMITTED"
  | "SUCCESS"
  | "REJECTED"
  | "TIMEOUT"
  | "FAILED"
  | "SKIPPED"
  | "RECONCILIATION_REQUIRED";

export interface CopyTradeRecord {
  id: string;
  master_order_id: string;
  follower_account_id: string;
  copy_event_id: string;
  symbol: string;
  exchange: string;
  side: "BUY" | "SELL";
  quantity: number;
  product_type: string;
  estimated_price?: string | null;
  client_order_id?: string | null;
  broker_order_id?: string | null;
  status: OrderStatus;
  failure_reason?: string | null;
  retry_count: number;
  queued_at: string;
  submitted_at?: string | null;
  completed_at?: string | null;
  created_at: string;
}

export interface TradeStats {
  total_trades_today: number;
  success_count: number;
  rejected_count: number;
  failed_count: number;
  pending_count: number;
}

export interface ReconciliationRecordItem {
  id: string;
  account_id: string;
  symbol: string;
  expected_qty: number;
  actual_qty: number;
  discrepancy_qty: number;
  run_at: string;
  resolved: boolean;
  notes?: string | null;
}

export interface EmergencyStopStatus {
  global_active: boolean;
}

export type QuantityMode =
  | "FIXED"
  | "RATIO"
  | "CAPITAL_PROPORTIONAL"
  | "RISK_BASED";

export interface DematAccount {
  id: string;
  accountType: AccountType;
  displayName: string;
  aliceUserId: string;
  isActive: boolean;
  isEmergencyStopped: boolean;
  sessionValid: boolean;
  lastAuthenticatedAt?: string;
  createdAt: string;
}

export interface CopySettings {
  id: string;
  followerAccountId: string;
  copyEnabled: boolean;
  quantityMode: QuantityMode;
  fixedQty?: number;
  ratio?: number;
  maxTradeValue?: number;
  maxDailyLoss?: number;
  maxExposure?: number;
  allowedSymbols?: string[];
}
