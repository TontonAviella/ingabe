import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";

// Public routes that don't require authentication
const isPublicRoute = createRouteMatcher([
  "/",
  "/sign-in(.*)",
  "/sign-up(.*)",
  "/auth(.*)",
  "/oauth(.*)",
]);

// Dev-only bypass for Claude Code preview panel.
// The preview panel hard-blocks non-localhost URLs (Google OAuth, Clerk
// hosted UI, etc.), so set NEXT_PUBLIC_DEV_BYPASS_AUTH=1 in noza-web/.env.local to
// skip auth.protect() entirely. Production unaffected — guard checks
// both the env flag AND NODE_ENV !== "production".
const DEV_BYPASS_AUTH =
  process.env.NEXT_PUBLIC_DEV_BYPASS_AUTH === "1" &&
  process.env.NODE_ENV !== "production";

export default clerkMiddleware(async (auth, request) => {
  if (DEV_BYPASS_AUTH) return;
  if (!isPublicRoute(request)) {
    await auth.protect();
  }
});

export const config = {
  matcher: [
    // Skip Next.js internals and all static files
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    // Always run for API routes
    "/(api|trpc)(.*)",
  ],
};
