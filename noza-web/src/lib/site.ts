// Sign-in happens in the Ingabe app (WorkOS); this site only links to it.
export const INGABE_URL = (process.env.NEXT_PUBLIC_INGABE_URL || "http://localhost:8000").replace(/\/$/, "");
export const SIGN_IN_URL = `${INGABE_URL}/auth/login`;

export const PHONES = [
  { label: "+250 783 922 314", href: "tel:+250783922314" },
  { label: "+250 780 480 682", href: "tel:+250780480682" },
];
