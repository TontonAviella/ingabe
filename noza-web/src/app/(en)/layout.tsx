import type { Metadata, Viewport } from "next";
import { RootHtml, siteMetadata } from "@/components/RootHtml";
import "../globals.css";

export const metadata: Metadata = siteMetadata("en");

export const viewport: Viewport = {
  themeColor: "#F6F1EB",
};

export default function EnglishLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <RootHtml locale="en">{children}</RootHtml>;
}
