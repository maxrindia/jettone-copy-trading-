"use client";

import { FormEvent, useState } from "react";
import { signIn } from "next-auth/react";
import { useRouter } from "next/navigation";
import { BarChart3, Loader2, Lock, ShieldCheck, User, Users, Zap } from "lucide-react";
import { CandlestickArt, WavesArt } from "@/components/auth/AuthBackdrop";
import { safeCallbackPath } from "@/lib/callback-url";

type FormState = "idle" | "submitting";

// Two fixed lines per label so the row wraps the same way at every width.
const FEATURES = [
  { icon: Users, lines: ["Multi-Account", "Copy Trading"] },
  { icon: BarChart3, lines: ["Real-time", "Order Sync"] },
  { icon: ShieldCheck, lines: ["Risk Management", "& Controls"] },
  { icon: Zap, lines: ["Reliable", "Execution"] },
] as const;

export default function SignInPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [formState, setFormState] = useState<FormState>("idle");

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);

    if (!email.trim() || !password) {
      setError("Please enter both username and password.");
      return;
    }
    if (email.includes("@")) {
      setError("Please enter your username, not an email address.");
      return;
    }

    setFormState("submitting");
    try {
      const result = await signIn("credentials", {
        email: email.trim(),
        password,
        redirect: false,
      });

      if (result?.error) {
        setError("Invalid username or password.");
        setFormState("idle");
        return;
      }

      // Back to the page the middleware bounced us from (same-origin paths only).
      const target = safeCallbackPath(new URLSearchParams(window.location.search).get("callbackUrl"), window.location.origin);
      router.replace(target);
      router.refresh();
    } catch {
      setError("Sign-in failed. Please try again.");
      setFormState("idle");
    }
  };

  return (
    <div className="relative min-h-[100dvh] overflow-hidden bg-white">
      {/* Decorative waves: behind everything, faded toward both sides, never interactive. Hidden on phones. */}
      <div
        className="pointer-events-none absolute inset-x-0 bottom-0 hidden h-[27vh] min-h-[150px] sm:block [mask-image:linear-gradient(to_right,transparent,black_18%,black_82%,transparent)]"
        aria-hidden
      >
        <WavesArt className="h-full w-full" />
      </div>

      <div className="relative mx-auto grid min-h-[100dvh] w-full max-w-[1800px] items-center gap-10 px-5 py-10 sm:px-8 md:grid-cols-[minmax(0,1fr)_clamp(340px,31vw,560px)] md:px-[clamp(2rem,5.5vw,6.25rem)]">
        {/* Left: brand + message, with the decorative chart in its own column (tablet and up) */}
        <section className="hidden md:grid md:-mt-[9vh] md:items-stretch min-[1180px]:grid-cols-[clamp(380px,29vw,520px)_minmax(0,1fr)] min-[1180px]:gap-4">
          <div>
            {/* Fixed height = the old wordmark + subtitle block, so nothing below it moves. The logo image
                (public/logo.png, 500x200 with transparent padding) is 1.5x the old wordmark; the relative
                offsets cancel its padding so the lettering keeps the old top-left position, and being
                relative they do not move the headline below. */}
            <div className="-mt-1 h-[48.25px]">
              <img src="/logo.png" alt="Jettone" className="relative block h-[81px] w-[203px] max-w-none -left-3 -top-[19px]" />
            </div>

            <h2 className="mt-[clamp(2.5rem,8vh,5.5rem)] text-[clamp(2.5rem,3.6vw,4rem)] font-extrabold leading-[1.06] tracking-tight text-[#111111]">
              Smart Wealth
              <span className="block text-[#E88900]">Smarter Future</span>
            </h2>
            <p className="mt-6 max-w-[30em] text-[clamp(0.95rem,1.22vw,1.375rem)] leading-relaxed text-[#6B7280]">
              Advanced copy trading infrastructure for disciplined and automated trading.
            </p>

            <ul className="mt-[clamp(2rem,6vh,3.5rem)] grid max-w-[520px] grid-cols-2 gap-x-2 gap-y-6 lg:grid-cols-4">
              {FEATURES.map(({ icon: Icon, lines }) => (
                <li key={lines.join(" ")} className="flex flex-col items-center gap-2.5 text-center">
                  <span className="flex h-12 w-12 items-center justify-center rounded-[10px] border border-orange-100 bg-orange-50 text-[#E88900] shadow-sm min-[1600px]:h-14 min-[1600px]:w-14">
                    <Icon className="h-5 w-5" aria-hidden />
                  </span>
                  <span className="text-[11px] leading-snug tracking-tight text-[#374151] min-[1600px]:text-[13px]">
                    {lines.map((line) => (
                      <span key={line} className="block whitespace-nowrap">
                        {line}
                      </span>
                    ))}
                  </span>
                </li>
              ))}
            </ul>
          </div>

          {/* Decorative candlestick art: its own column beside the text, so it can never sit on the feature row. */}
          <div
            className="pointer-events-none relative -mr-6 hidden min-[1180px]:block [mask-image:linear-gradient(to_right,transparent,black_16%,black_88%,transparent)]"
            aria-hidden
          >
            <CandlestickArt className="absolute inset-x-0 -inset-y-[4%] h-[108%] w-full" />
          </div>
        </section>

        {/* Right: the login card (the only thing on mobile) */}
        <main className="mx-auto w-full max-w-[420px] md:max-w-none">
          <div className="w-full p-6 md:border-l md:border-[#E5E7EB] md:p-[clamp(1.5rem,2.8vw,3.1rem)]">
            {/* Slightly narrower than the column so the fields do not feel stretched. */}
            <div className="mx-auto w-full md:w-[88%]">
            {/* Phones: the official logo only (public/logo.png, 500x200 with transparent padding - the negative
                margins trim it so the spacing around the lettering is even). */}
            <div className="mb-6 md:hidden">
              <img src="/logo.png" alt="Jettone" className="mx-auto block h-[72px] w-[180px] max-w-full -my-3" />
            </div>
            {/* md+: the hero beside the form already carries the logo, so the form just says what it is. */}
            <h1 className="mb-7 hidden text-center text-2xl font-extrabold tracking-tight text-[#111111] md:block min-[1600px]:mb-10 min-[1600px]:text-3xl">
              Log In
            </h1>

            <form onSubmit={handleSubmit} className="space-y-4 min-[1600px]:space-y-6" noValidate>
              <div className="space-y-1.5">
                <label htmlFor="username" className="block text-xs font-semibold text-[#374151] uppercase tracking-wide">
                  Username
                </label>
                <div className="relative">
                  <User className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[#6B7280]" aria-hidden />
                  <input
                    id="username"
                    name="username"
                    type="text"
                    autoComplete="username"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="Enter username"
                    disabled={formState === "submitting"}
                    className="h-11 min-[1600px]:h-12 w-full rounded-lg border border-[#E5E7EB] bg-white pl-10 pr-3 text-sm text-[#111111] placeholder-[#9CA3AF] focus:border-transparent focus:outline-none focus:ring-2 focus:ring-[#111111] disabled:opacity-60"
                  />
                </div>
              </div>

              <div className="space-y-1.5">
                <label htmlFor="password" className="block text-xs font-semibold text-[#374151] uppercase tracking-wide">
                  Password
                </label>
                <div className="relative">
                  <Lock className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[#6B7280]" aria-hidden />
                  <input
                    id="password"
                    name="password"
                    type="password"
                    autoComplete="current-password"
                    required
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="••••••••"
                    disabled={formState === "submitting"}
                    className="h-11 min-[1600px]:h-12 w-full rounded-lg border border-[#E5E7EB] bg-white pl-10 pr-3 text-sm text-[#111111] placeholder-[#9CA3AF] focus:border-transparent focus:outline-none focus:ring-2 focus:ring-[#111111] disabled:opacity-60"
                  />
                </div>
              </div>

              {error && (
                <p role="alert" className="text-xs font-medium text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
                  {error}
                </p>
              )}

              <div className="pt-1.5">
                <button
                  type="submit"
                  disabled={formState === "submitting"}
                  className="inline-flex h-12 min-[1600px]:h-14 w-full items-center justify-center gap-2 rounded-lg bg-[#E88900] text-sm font-semibold text-white shadow-[0_6px_18px_rgba(232,137,0,0.16)] transition-all hover:bg-[#D97700] hover:shadow-[0_8px_22px_rgba(232,137,0,0.28)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[#E88900] focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {formState === "submitting" ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin" aria-hidden />
                      Signing in…
                    </>
                  ) : (
                    <>
                      <ShieldCheck className="w-4 h-4" aria-hidden />
                      Sign In
                    </>
                  )}
                </button>
              </div>
            </form>

            <div className="mt-7 flex items-center gap-3 text-[11px] text-[#9CA3AF] min-[1600px]:mt-11 min-[1600px]:text-xs">
              <span className="h-px flex-1 bg-[#E5E7EB]" aria-hidden />
              <p>Authorized owner access only.</p>
              <span className="h-px flex-1 bg-[#E5E7EB]" aria-hidden />
            </div>
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}
