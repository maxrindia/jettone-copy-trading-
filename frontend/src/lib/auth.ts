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
export async function verifyCredentials(
  email: string | undefined,
  password: string | undefined,
): Promise<{ id: string; name: string; email: string; role: "admin" } | null> {
  if (!password) {
    return null;
  }

  let adminPasswordHash = process.env.ADMIN_PASSWORD_HASH;
  if (!adminPasswordHash) {
    return null;
  }
  adminPasswordHash = adminPasswordHash.trim().replace(/^['"]|['"]$/g, "").replaceAll("$$", "$");

  const isValidPassword = await bcrypt.compare(password.trim(), adminPasswordHash);
  if (!isValidPassword) {
    return null;
  }

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
