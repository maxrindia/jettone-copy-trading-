import { brokerLabel } from "@/lib/broker-matrix";
import { canSetUp, connectionView, identityNotice, roleChips, type AccountStatusFields, type Tone } from "@/lib/account-status";

const TONE: Record<Tone, string> = {
  ok: "bg-green-50 text-green-800 border-green-200",
  warn: "bg-amber-50 text-amber-800 border-amber-200",
  muted: "bg-[#F7F7F7] text-[#374151] border-[#E5E7EB]",
};

/**
 * One account's broker, connection state and copy-trading roles, as the engine reports them. The same markup is used by the grid
 * cards and the list rows, so the two views never disagree. Nothing here enables anything: a blocked role says why, in the engine's words.
 */
export default function AccountBrokerBadges({ account }: { account: AccountStatusFields }) {
  const conn = connectionView(account);
  const roles = roleChips(account.broker_capabilities);
  const setup = canSetUp(account.broker_capabilities);
  const allBlocked = roles.every((r) => !r.available);
  const identity = identityNotice(account);

  return (
    <>
      <span
        data-testid="account-broker"
        className="px-2 py-0.5 rounded-lg text-[10px] font-bold border bg-white text-[#111111] border-[#E5E7EB]"
      >
        {brokerLabel((account.broker || "alicebluenew").toLowerCase())}
      </span>
      {conn.status !== "CONNECTED" && conn.status !== "DISCONNECTED" && (
        <span
          data-testid="account-connection-issue"
          data-status={conn.status}
          title={conn.detail}
          className={`px-2 py-0.5 rounded-lg text-[10px] font-bold border ${TONE[conn.tone]}`}
        >
          {conn.label}
        </span>
      )}
      {identity && (
        <span
          data-testid="account-identity-notice"
          title={identity}
          className="px-2 py-0.5 rounded-lg text-[10px] font-semibold border bg-amber-50 text-amber-800 border-amber-200"
        >
          Identity not confirmed
        </span>
      )}
      {roles.map((r) => (
        <span
          key={r.role}
          data-testid={`account-role-${r.role.toLowerCase()}`}
          data-available={r.available ? "yes" : "no"}
          title={r.available ? `${r.role} copy trading is available for this broker.` : r.reason || ""}
          className={`px-2 py-0.5 rounded-lg text-[10px] font-semibold border ${
            r.available ? "bg-green-50 text-green-800 border-green-200" : "bg-[#F7F7F7] text-[#6B7280] border-[#E5E7EB]"
          }`}
        >
          {r.role}: {r.available ? "available" : "blocked"}
        </span>
      ))}
      {setup && allBlocked && (
        <span data-testid="account-setup-only" title="This account can be set up and signed in, but this broker cannot take part in copy trading yet." className="sr-only">
          Setup only
        </span>
      )}
    </>
  );
}
