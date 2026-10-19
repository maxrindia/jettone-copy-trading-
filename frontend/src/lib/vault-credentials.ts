/**
 * Brokers whose secrets live in the engine's encrypted, write-only credential vault
 * (never in the account's `notes`, which the account list returns). The engine
 * decides which brokers may be used at all (GET /api/v1/accounts/supported-brokers);
 * this only shapes what is sent once the user has chosen one.
 *
 *   angelone: api_key, pin, totp_secret
 *   groww:    api_key, api_secret
 */
export type VaultBroker = "angelone" | "groww";

export interface VaultFields {
  /** SmartAPI key (Angel One) or API key (Groww). */
  apiKey: string;
  /** PIN (Angel One) or API secret (Groww). */
  secret: string;
  /** TOTP secret (Angel One only). */
  totpSecret: string;
}

export function usesVault(broker: string | null | undefined): broker is VaultBroker {
  return broker === "angelone" || broker === "groww";
}

function required(broker: VaultBroker, f: VaultFields): string[] {
  return broker === "angelone" ? [f.apiKey, f.secret, f.totpSecret] : [f.apiKey, f.secret];
}

export function hasAnyVaultCredential(broker: VaultBroker, f: VaultFields): boolean {
  return required(broker, f).some((v) => v.trim());
}

export function vaultCredentialBody(broker: VaultBroker, f: VaultFields): Record<string, string> {
  if (broker === "angelone") {
    return { api_key: f.apiKey.trim(), pin: f.secret.trim(), totp_secret: f.totpSecret.trim() };
  }
  return { api_key: f.apiKey.trim(), api_secret: f.secret.trim() };
}

/** What blocks saving, or null. Credentials are all-or-nothing; a new account needs them. */
export function vaultCredentialProblem(broker: VaultBroker, f: VaultFields, isNewAccount: boolean): string | null {
  if (!(isNewAccount || hasAnyVaultCredential(broker, f))) return null;
  if (required(broker, f).every((v) => v.trim())) return null;
  return broker === "angelone"
    ? "Angel One needs the SmartAPI key, PIN and TOTP secret."
    : "Groww needs the API key and API secret.";
}

/** The engine route that logs the account in with its stored credentials. */
export function brokerLoginPath(accountId: string): string {
  return `/api/v1/accounts/${accountId}/broker-login`;
}
