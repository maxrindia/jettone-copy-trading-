import { NextAuthOptions } from "next-auth";
import CredentialsProvider from "next-auth/providers/credentials";
import bcrypt from "bcryptjs";

export function isAuthConfigured(): boolean {
  return true;
}

/**
 * Verifies the single-owner admin credential against the bcrypt hash in
 * ADMIN_PASSWORD_HASH (generated via scripts/hash-password.mjs). Exported
 * standalone, separate from the CredentialsProvider wiring below, so it can
 * be unit tested without going through NextAuth's request/session machinery.
 *
 * Fails closed: no ADMIN_PASSWORD_HASH configured means no one can log in.
 */
// Brute-force brake for a login that may be reachable over the internet: after
// MAX_FAILURES wrong attempts in a row every attempt is refused (without even
// comparing the password) until LOCKOUT_MS has passed. Single-owner app, so one
// process-wide counter is enough; a restart clears it.
const MAX_FAILURES = 5;
const LOCKOUT_MS = 5 * 60 * 1000;
let consecutiveFailures = 0;
let lockedUntil = 0;

export function _resetLoginThrottleForTests(): void {
  consecutiveFailures = 0;
  lockedUntil = 0;
}

function allowedUsernames(): string[] {
  const configured = (process.env.ADMIN_EMAIL || "").trim().toLowerCase();
  return ["admin", "admin@jettone.local", ...(configured ? [configured] : [])];
}

export async function verifyCredentials(
  email: string | undefined,
  password: string | undefined,
): Promise<{ id: string; name: string; email: string; role: "admin" } | null> {
  if (!password) {
    return null;
  }
  if (Date.now() < lockedUntil) {
    return null;
  }
  const fail = (): null => {
    consecutiveFailures += 1;
    if (consecutiveFailures >= MAX_FAILURES) {
      lockedUntil = Date.now() + LOCKOUT_MS;
      consecutiveFailures = 0;
    }
    return null;
  };
  // The username must be the owner's ("admin"); only the password was checked before.
  const username = (email || "admin").trim().toLowerCase();
  if (!allowedUsernames().includes(username)) {
    return fail();
  }

  let adminPasswordHash = process.env.ADMIN_PASSWORD_HASH;
  if (!adminPasswordHash) {
    return null;
  }
  adminPasswordHash = adminPasswordHash.trim().replace(/^['"]|['"]$/g, "").replaceAll("$$", "$");

  const isValidPassword = await bcrypt.compare(password.trim(), adminPasswordHash);
  if (!isValidPassword) {
    return fail();
  }
  consecutiveFailures = 0;

  const inputUser = (email || "admin").trim();
  return {
    id: "1",
    name: "Platform Owner",
    email: inputUser.includes("@") ? inputUser : "admin@jettone.local",
    role: "admin",
  };
}

export const authOptions: NextAuthOptions = {
  providers: [
    CredentialsProvider({
      name: "Credentials",
      credentials: {
        email: { label: "Username or Email", type: "text" },
        password: { label: "Password", type: "password" },
      },
      async authorize(credentials) {
        return verifyCredentials(credentials?.email, credentials?.password);
      },
    }),
  ],
  session: {
    strategy: "jwt",
    maxAge: 24 * 60 * 60,
  },
  callbacks: {
    async jwt({ token, user }) {
      if (user) {
        token.role = user.role;
      }
      return token;
    },
    async session({ session, token }) {
      if (session.user) {
        session.user.role = token.role;
      }
      return session;
    },
  },
  pages: {
    signIn: "/auth/signin",
  },
};
