import type { Metadata } from "next";
import { Inter } from "next/font/google";
import { ClerkProvider } from "@clerk/nextjs";
import "./globals.css";

export const dynamic = "force-dynamic";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Noza Agricultural Intelligence",
  description: "AI-powered agricultural intelligence platform for crop health analysis, drone imagery processing, and farm management",
  keywords: ["agriculture", "AI", "crop health", "drone imagery", "farm management", "NDVI", "satellite imagery"],
};

// Ingabe app URL(s) that are allowed to redirect back after sign-in
const allowedRedirectOrigins = process.env.NEXT_PUBLIC_INGABE_URL
  ? [process.env.NEXT_PUBLIC_INGABE_URL]
  : ["http://localhost:8000"];

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <ClerkProvider
      signInUrl="/sign-in"
      signUpUrl="/sign-up"
      allowedRedirectOrigins={allowedRedirectOrigins}
    >
      <html lang="en">
        <head>
          <link
            rel="stylesheet"
            href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
            integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
            crossOrigin=""
          />
        </head>
        <body className={inter.className}>{children}</body>
      </html>
    </ClerkProvider>
  );
}
