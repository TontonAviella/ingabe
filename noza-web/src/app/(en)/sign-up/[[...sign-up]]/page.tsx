import { redirect } from "next/navigation";

// Sign-in lives in the Ingabe app (WorkOS); keep old links working.
export default function Page() {
  const app = (process.env.NEXT_PUBLIC_INGABE_URL || "http://localhost:8000").replace(/\/$/, "");
  redirect(`${app}/auth/login?screen_hint=sign-up`);
}
