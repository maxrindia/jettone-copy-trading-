/**
 * Angel One credentials never go into the account's `notes` (which the account
 * list returns). They are sent once to the write-only vault endpoint and are
 * not shown again.
 */
export interface AngelCredentialFields {
  apiKey: string;
  pin: string;
  totpSecret: string;
}

export type AngelCredentialBody = { api_key: string; pin: string; totp_secret: string };

export function hasAnyAngelCredential(f: AngelCredentialFields): boolean {
  return Boolean(f.apiKey.trim() || f.pin.trim() || f.totpSecret.trim());
}

export function angelCredentialsComplete(f: AngelCredentialFields): boolean {
  return Boolean(f.apiKey.trim() && f.pin.trim() && f.totpSecret.trim());
}

export function buildAngelCredentialBody(f: AngelCredentialFields): AngelCredentialBody {
  return { api_key: f.apiKey.trim(), pin: f.pin.trim(), totp_secret: f.totpSecret.trim() };
}

/** What blocks saving, or null. Credentials are all-or-nothing; a new account needs them. */
export function angelCredentialProblem(f: AngelCredentialFields, isNewAccount: boolean): string | null {
  if (isNewAccount || hasAnyAngelCredential(f)) {
    if (!angelCredentialsComplete(f)) return "Angel One needs the SmartAPI key, PIN and TOTP secret.";
  }
  return null;
}
