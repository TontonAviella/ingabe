"use client";

import {
  Sprout,
  MapPin,
  Cloud,
  TrendingUp,
  Satellite,
  Cpu,
  BarChart3,
  MessageSquare
} from "lucide-react";
import {
  SignedIn,
  SignedOut,
  SignInButton,
  UserButton,
} from "@clerk/nextjs";

const INGABE_URL = process.env.NEXT_PUBLIC_INGABE_URL || "https://gis.nozalabs.rw";

export default function Home() {
  return (
    <div className="min-h-screen bg-[#f7f5f2]">
      {/* Navigation - Exact Anthropic style */}
      <nav className="bg-[#f7f5f2] border-b border-[#e5e3df]">
        <div className="max-w-[90rem] mx-auto px-8 lg:px-12">
          <div className="flex justify-between items-center h-20">
            {/* Logo - Anthropic style text only */}
            <div>
              <span className="text-2xl font-medium tracking-tight text-[#1a1816]">nozalabs</span>
            </div>

            {/* Auth */}
            <div className="flex items-center gap-4">
              <SignedOut>
                <SignInButton mode="modal">
                  <button className="inline-flex items-center justify-center px-5 py-2.5 text-sm font-medium text-[#f7f5f2] bg-[#1a1816] rounded-full hover:bg-[#2d2b28] transition-colors cursor-pointer">
                    Sign In
                  </button>
                </SignInButton>
              </SignedOut>
              <SignedIn>
                <a
                  href={INGABE_URL}
                  className="inline-flex items-center justify-center px-5 py-2.5 text-sm font-medium text-[#1a1816] border border-[#1a1816] rounded-full hover:bg-[#1a1816] hover:text-[#f7f5f2] transition-colors"
                >
                  Open Ingabe
                </a>
                <UserButton afterSignOutUrl="/" />
              </SignedIn>
            </div>
          </div>
        </div>
      </nav>

      {/* Hero Section - Anthropic hero style */}
      <section className="max-w-[90rem] mx-auto px-8 lg:px-12 pt-24 pb-32">
        <div className="grid lg:grid-cols-2 gap-16 items-center">
          {/* Left: Text */}
          <div className="space-y-8">
            <h1 className="text-[4rem] lg:text-[5rem] xl:text-[6rem] font-normal text-[#1a1816] leading-[0.95] tracking-[-0.02em]">
              Agricultural{" "}
              <span className="underline decoration-[#166534] decoration-4 underline-offset-8">
                intelligence
              </span>{" "}
              that puts farmers at the frontier
            </h1>
            <p className="text-xl lg:text-2xl text-[#3d3935] leading-relaxed max-w-2xl">
              AI-powered insights for modern farming. Nozalabs builds tools to help farmers make better decisions through advanced crop analysis and precision agriculture.
            </p>
          </div>

          {/* Right: Illustration placeholder */}
          <div className="hidden lg:flex items-center justify-center">
            <div className="w-full h-[400px] flex items-center justify-center">
              <Sprout className="w-64 h-64 text-[#166534] opacity-20" strokeWidth={1} />
            </div>
          </div>
        </div>
      </section>

      {/* Mission Statement Section */}
      <section className="max-w-[90rem] mx-auto px-8 lg:px-12 py-24">
        <div className="grid lg:grid-cols-2 gap-16">
          <div>
            <h2 className="text-4xl lg:text-5xl font-normal text-[#1a1816] leading-tight mb-8">
              At Nozalabs, we build agricultural intelligence for better farming outcomes.
            </h2>
          </div>
          <div className="space-y-6 text-lg text-[#3d3935] leading-relaxed">
            <p>
              While farming faces unprecedented challenges, we believe technology can help. Our platform combines satellite imagery, drone processing, and AI analysis to give farmers actionable insights.
            </p>
            <p>
              That's why we focus on building tools that put farmers first. Through our product Noza, we aim to show what responsible agricultural technology looks like in practice.
            </p>
          </div>
        </div>
      </section>

      {/* Capabilities Cards Section */}
      <section className="max-w-[90rem] mx-auto px-8 lg:px-12 py-24">
        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-6">
          <CapabilityCard
            bgColor="bg-[#e8e0d5]"
            icon={<Satellite className="w-12 h-12 text-[#1a1816]" strokeWidth={1.5} />}
            title="Satellite Imagery"
            description="Access global satellite data for comprehensive field analysis"
          />
          <CapabilityCard
            bgColor="bg-[#c9e4de]"
            icon={<Cpu className="w-12 h-12 text-[#1a1816]" strokeWidth={1.5} />}
            title="Drone Processing"
            description="Advanced aerial imagery analysis"
          />
          <CapabilityCard
            bgColor="bg-[#d4d9e8]"
            icon={<TrendingUp className="w-12 h-12 text-[#1a1816]" strokeWidth={1.5} />}
            title="NDVI Analysis"
            description="40+ vegetation indices for crop health"
          />
          <CapabilityCard
            bgColor="bg-[#e8e0d5]"
            icon={<MessageSquare className="w-12 h-12 text-[#1a1816]" strokeWidth={1.5} />}
            title="AI Assistant"
            description="Intelligent agricultural advisor"
          />
          <CapabilityCard
            bgColor="bg-[#c9e4de]"
            icon={<MapPin className="w-12 h-12 text-[#1a1816]" strokeWidth={1.5} />}
            title="Field Mapping"
            description="Precise boundary management"
          />
          <CapabilityCard
            bgColor="bg-[#d4d9e8]"
            icon={<Cloud className="w-12 h-12 text-[#1a1816]" strokeWidth={1.5} />}
            title="Weather Integration"
            description="Real-time weather forecasting"
          />
        </div>
      </section>

      {/* CTA Section */}
      <section className="max-w-[90rem] mx-auto px-8 lg:px-12 py-32">
        <div className="bg-[#e8e0d5] rounded-3xl p-16 text-center">
          <h2 className="text-4xl lg:text-5xl font-normal text-[#1a1816] mb-8">
            Ready to transform your farming operations?
          </h2>
          <SignedOut>
            <SignInButton mode="modal">
              <button className="inline-flex items-center justify-center px-8 py-4 text-base font-medium text-[#f7f5f2] bg-[#1a1816] rounded-full hover:bg-[#2d2b28] transition-all hover:scale-105 cursor-pointer">
                Get Started with Noza
              </button>
            </SignInButton>
          </SignedOut>
          <SignedIn>
            <a
              href={INGABE_URL}
              className="inline-flex items-center justify-center px-8 py-4 text-base font-medium text-[#f7f5f2] bg-[#1a1816] rounded-full hover:bg-[#2d2b28] transition-all hover:scale-105"
            >
              Open Ingabe
            </a>
          </SignedIn>
        </div>
      </section>

      {/* Footer */}
      <footer className="border-t border-[#e5e3df] py-12">
        <div className="max-w-[90rem] mx-auto px-8 lg:px-12">
          <div className="flex flex-col md:flex-row justify-between items-center gap-4">
            <p className="text-sm text-[#6b6662]">© 2025 Nozalabs</p>
            <div className="flex items-center gap-6 text-sm text-[#6b6662]">
              <a href="tel:+250783922314" className="hover:text-[#1a1816] transition-colors">
                +250 783 922 314
              </a>
              <span>•</span>
              <a href="tel:+250780480682" className="hover:text-[#1a1816] transition-colors">
                +250 780 480 682
              </a>
            </div>
          </div>
        </div>
      </footer>
    </div>
  );
}

function CapabilityCard({
  bgColor,
  icon,
  title,
  description
}: {
  bgColor: string;
  icon: React.ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className={`${bgColor} rounded-3xl p-8 space-y-6 hover:scale-[1.02] transition-transform`}>
      <div>{icon}</div>
      <div className="space-y-3">
        <h3 className="text-xl font-medium text-[#1a1816]">{title}</h3>
        <p className="text-base text-[#3d3935] leading-relaxed">{description}</p>
      </div>
    </div>
  );
}
