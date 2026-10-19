import { brokerCapabilities, type CatalogEntry } from "@/lib/broker-support";

/**
 * Shows, for a selectable non-Alice broker, what the engine currently allows: credential setup and login,
 * Master eligibility and Child eligibility are separate. It reports the backend catalog; it enforces nothing.
 * Alice Blue (fully supported) renders nothing, so its screen is unchanged.
 */
export default function BrokerCapabilityStrip({
  broker,
  catalog,
}: {
  broker: string;
  catalog: readonly CatalogEntry[] | null;
}) {
  const entry = catalog?.find((c) => c.id === broker.trim().toLowerCase());
  if (!entry || !entry.selectable || entry.support === "SUPPORTED") return null;
  const caps = brokerCapabilities(broker, catalog);
  const items: { label: string; ok: boolean }[] = [
    { label: "Setup & login", ok: caps.login },
    { label: "Master", ok: caps.asMaster },
    { label: "Child", ok: caps.asChild },
  ];
  return (
    <div role="status" data-testid="broker-capabilities" className="p-3 rounded-[10px] bg-[#F9FAFB] border border-[#E5E7EB] space-y-2">
      <ul className="flex flex-wrap gap-2">
        {items.map((i) => (
          <li
            key={i.label}
            data-capability={i.label}
            data-available={i.ok ? "yes" : "no"}
            className={`text-[11px] font-semibold px-2 py-0.5 rounded border ${
              i.ok ? "bg-green-50 border-green-200 text-green-800" : "bg-gray-100 border-gray-200 text-gray-600"
            }`}
          >
            {i.label}: {i.ok ? "available" : "not available"}
          </li>
        ))}
      </ul>
      {caps.note && <p className="text-[11px] leading-4 text-[#6B7280]">{caps.note}</p>}
    </div>
  );
}
