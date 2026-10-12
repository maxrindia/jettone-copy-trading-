import { NextResponse } from "next/server";
import { withAuth } from "next-auth/middleware";

/**
 * Route protection (NextAuth session). Same behaviour as plain `withAuth` for
 * pages - no session means a redirect to /auth/signin - with two fixes:
 *
 *  - API calls without a session get a 401 JSON error. A redirect made fetch()
 *    follow it to the sign-in HTML page, which then blew up as
 *    "Unexpected token '<' ... is not valid JSON" in the calling page.
 *  - Every protected response is `no-store`. Prerendered pages otherwise carry
 *    `s-maxage=31536000`, so after logout the browser's Back button could
 *    restore the protected page (and its polling) from cache.
 */
const NO_STORE = "no-store, max-age=0, must-revalidate";

export default withAuth(
  function middleware(req) {
    if (!req.nextauth.token) {
      if (req.nextUrl.pathname.startsWith("/api/")) {
        return NextResponse.json(
          { success: false, error: { code: "UNAUTHENTICATED", message: "Not signed in" } },
          { status: 401, headers: { "Cache-Control": NO_STORE } },
        );
      }
      const signIn = new URL("/auth/signin", req.url);
      signIn.searchParams.set("callbackUrl", req.nextUrl.pathname + req.nextUrl.search);
      const redirect = NextResponse.redirect(signIn);
      redirect.headers.set("Cache-Control", NO_STORE);
      return redirect;
    }
    const res = NextResponse.next();
    res.headers.set("Cache-Control", NO_STORE);
    return res;
  },
  {
    secret: process.env.NEXTAUTH_SECRET,
    pages: {
      signIn: "/auth/signin",
    },
    // The decision is made in the handler above (it needs to answer APIs with
    // JSON); the token is still decoded and verified by NextAuth here.
    callbacks: { authorized: () => true },
  },
);

export const config = {
  matcher: [
    "/((?!api/auth|auth/signin|_next/static|_next/image|favicon.ico|.*\.(?:svg|png|jpg|jpeg|gif|ico|webp)$).*)",
  ],
};
