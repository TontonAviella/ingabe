import { SignIn } from "@clerk/nextjs";
import Link from "next/link";

// Dev-only: skip the Clerk widget entirely so the preview panel can render
// this route without hitting the accounts.google.com OAuth redirect block.
// Mirrors middleware.ts — only activates when both flags align.
const DEV_BYPASS_AUTH =
  process.env.NEXT_PUBLIC_DEV_BYPASS_AUTH === "1" &&
  process.env.NODE_ENV !== "production";

export default function SignInPage() {
  if (DEV_BYPASS_AUTH) {
    return (
      <div className="min-h-screen bg-[#f7f5f2] flex items-center justify-center">
        <div className="max-w-md text-center px-6">
          <h1 className="text-2xl font-semibold mb-3">Dev mode — sign-in skipped</h1>
          <p className="text-gray-600 mb-6">
            Clerk auth is bypassed via <code className="bg-gray-100 px-1 rounded">NEXT_PUBLIC_DEV_BYPASS_AUTH=1</code>.
            Pages render in their signed-out shell; protected logic is unreachable in this mode.
          </p>
          <Link
            href="/"
            className="inline-block px-5 py-2 rounded-md bg-black text-white hover:bg-gray-800"
          >
            Back to home
          </Link>
        </div>
      </div>
    );
  }
  return (
    <div className="min-h-screen bg-[#f7f5f2] flex items-center justify-center">
      <SignIn />
    </div>
  );
}
