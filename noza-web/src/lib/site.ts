// This site's own public address, for search engines (canonical and language links).
export const SITE_URL = (process.env.NEXT_PUBLIC_SITE_URL || "https://nozalabs.rw").replace(/\/$/, "");

// Sign-in happens in the Ingabe app (WorkOS); this site only links to it.
export const INGABE_URL = (process.env.NEXT_PUBLIC_INGABE_URL || "http://localhost:8000").replace(/\/$/, "");
export const SIGN_IN_URL = `${INGABE_URL}/auth/login`;
// Ingabe is invite-only: people without an invitation ask here and Noza Labs staff approve or decline.
export const REQUEST_ACCESS_URL = `${INGABE_URL}/request-access`;

// The app's sign-in step and Request access page speak English and French: French pages keep readers in French.
export const signInUrl = (locale: "en" | "fr") => (locale === "fr" ? `${SIGN_IN_URL}?lang=fr` : SIGN_IN_URL);
export const requestAccessUrl = (locale: "en" | "fr") =>
  locale === "fr" ? `${REQUEST_ACCESS_URL}?lang=fr` : REQUEST_ACCESS_URL;

export const PHONES = [
  { label: "+250 783 922 314", href: "tel:+250783922314" },
  { label: "+250 780 480 682", href: "tel:+250780480682" },
];
