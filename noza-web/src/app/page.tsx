import { HeroWorld, WorldTile } from "@/components/MiniWorld";
import { MotionProvider, PauseButton } from "@/components/MotionProvider";
import { Arrow, Nav, Wordmark } from "@/components/Nav";
import { Parallax } from "@/components/Parallax";
import { Reveal } from "@/components/Reveal";
import { SageExample } from "@/components/SageExample";
import { TwoLooks } from "@/components/TwoLooks";
import { WireDrawing } from "@/components/WireDrawing";
import { Words } from "@/components/Words";
import { WorldsStrip } from "@/components/WorldsStrip";
import { INGABE_URL, PHONES, SIGN_IN_URL } from "@/lib/site";
import type { ModelName } from "@/lib/wire3d";

const FACTS = [
  { k: "Drone photos", v: "3 cm a pixel" },
  { k: "Satellite", v: "Every 5 days" },
  { k: "Rainfall", v: "Daily, since 1981" },
  { k: "Crop names", v: "Two looks must agree" },
];

type Card = { model: ModelName; tag: string; title: string; text: string; live: boolean; wide?: boolean };

const FARMS: Card[] = [
  {
    model: "crops",
    tag: "Crop map",
    title: "Which crop is in each plot",
    text: "Two separate looks at every plot. When they agree we name the crop; when they don't, we say not sure and show both guesses.",
    live: true,
  },
  {
    model: "count",
    tag: "Plant count",
    title: "How many plants stand",
    text: "Plants counted plot by plot, given as a range we can stand behind, not a single proud number.",
    live: true,
  },
  {
    model: "gaps",
    tag: "Gaps",
    title: "Where the crop is missing",
    text: "Open soil inside a plot is measured, and flagged only when it is a real share of the plot.",
    live: true,
  },
];

const LINES: Card[] = [
  {
    model: "tower",
    tag: "Towers",
    title: "Tower and insulator checks",
    text: "Each tower seen from the same angles every flight, so a missing part or new damage stands out.",
    live: false,
  },
  {
    model: "vegetation",
    tag: "Vegetation",
    title: "Trees growing into the line",
    text: "Tree height against the wire, span by span, so crews cut where it matters first.",
    live: false,
  },
  {
    model: "corridor",
    tag: "Right of way",
    title: "What is inside the corridor",
    text: "New houses, fields and roads inside the line's corridor, found before they become a danger.",
    live: false,
  },
];

export default function Home() {
  return (
    <MotionProvider>
      <Nav />
      <main id="top">
        {/* ---------- Hero ---------- */}
        <section className="relative overflow-hidden pt-16">
          <div className="relative mx-auto max-w-page px-5 sm:px-8 lg:flex lg:min-h-[calc(100vh-64px-146px)] lg:items-center lg:px-12">
            <div className="relative z-10 pt-10 lg:max-w-[36rem] lg:pb-16 lg:pt-0">
              <p className="arrive font-mono text-[12px] uppercase tracking-label text-mocha">Noza Labs · Kigali</p>
              <h1 className="mt-6 font-display text-[clamp(3rem,6.4vw,6.4rem)] leading-[0.93] tracking-[-0.02em] text-bitter">
                <Words text="We read the land, and the *lines* that cross it." start={150} />
              </h1>
              <p className="arrive mt-7 max-w-[28rem] text-[18px] leading-relaxed text-mocha sm:text-[19px]" style={{ animationDelay: "800ms" }}>
                Drone and satellite intelligence for Rwanda&apos;s farms, towers and power lines. Every answer says how sure it is.
              </p>
              <div className="arrive mt-9 flex flex-wrap items-center gap-6" style={{ animationDelay: "1000ms" }}>
                <a
                  href="#contact"
                  className="group inline-flex items-center gap-4 rounded-full bg-bitter py-4 pl-8 pr-7 text-[15px] font-medium text-cream transition-colors hover:bg-bark"
                >
                  Talk to us
                  <Arrow className="transition-transform duration-300 group-hover:translate-x-1" />
                </a>
                <a href={INGABE_URL} className="link-line inline-flex items-center gap-2 pb-0.5 text-[15px] text-bitter">
                  Open Ingabe <Arrow diagonal />
                </a>
              </div>
            </div>
          </div>
          <Parallax
            className="relative -mt-4 aspect-[1.05] w-full lg:absolute lg:bottom-[96px] lg:left-[33%] lg:right-[-6%] lg:[mask-image:linear-gradient(to_right,transparent_0,black_12%)] lg:top-16 lg:mt-0 lg:aspect-auto lg:w-auto"
          >
            <div className="arrive absolute inset-0" style={{ animationDelay: "300ms" }}>
              <HeroWorld className="absolute inset-0 h-full w-full" />
            </div>
          </Parallax>
          <div className="relative z-10 mx-auto hidden h-[50px] max-w-page items-center gap-5 px-12 lg:flex">
            <span className="h-px w-12 bg-bitter/40" />
            <p className="font-mono text-[11px] uppercase leading-[1.7] tracking-[0.16em] text-mocha">
              See it from above.
              <br />
              Answer it on the ground.
            </p>
          </div>
          <div className="border-t border-bitter/10">
            <dl className="mx-auto grid max-w-page grid-cols-2 px-5 sm:px-8 lg:grid-cols-4 lg:px-12">
              {FACTS.map((f, k) => (
                <Reveal key={f.k} delay={k * 80} className={`py-6 ${k % 2 ? "pl-5 sm:pl-8" : ""} ${k > 0 ? "lg:border-l lg:border-bitter/10 lg:pl-8" : ""}`}>
                  <dt className="font-mono text-[10.5px] uppercase tracking-label text-mocha">{f.k}</dt>
                  <dd className="mt-2 text-[17px] text-bitter">{f.v}</dd>
                </Reveal>
              ))}
            </dl>
          </div>
        </section>

        {/* ---------- What we read ---------- */}
        <section id="read" className="scroll-mt-16 bg-night py-24 text-sand sm:py-32">
          <div className="mx-auto max-w-page px-5 sm:px-8 lg:px-12">
            <div className="flex flex-col gap-8 lg:flex-row lg:items-end lg:justify-between">
              <Reveal>
                <p className="font-mono text-[11px] uppercase tracking-label text-caramel">What we read</p>
                <h2 className="mt-5 font-display text-[clamp(2.6rem,5vw,4.6rem)] leading-[0.98] tracking-[-0.015em]">What one flight can tell you.</h2>
                <p className="mt-5 max-w-xl text-[17px] leading-relaxed text-latte">
                  We study where answers go wrong in the field, then build the checks that keep them right.
                </p>
              </Reveal>
              <PauseButton className="self-start text-latte lg:self-end" />
            </div>

            <Group label="Farms" note="Live in Ingabe" cards={FARMS} />
            <Group label="Towers & power lines" note="In development · pilot flights open" cards={LINES} />

            <Reveal className="mt-12">
              <a href="#contact" className="group inline-flex items-center gap-3 text-[15px] text-sand">
                <span className="link-line pb-0.5">Tell us what you need to see</span>
                <Arrow className="transition-transform duration-300 group-hover:translate-x-1" />
              </a>
            </Reveal>
          </div>
        </section>

        {/* ---------- Where we fly ---------- */}
        <section id="where" className="scroll-mt-0 bg-cream">
          <WorldsStrip
            header={
              <Reveal className="max-w-3xl">
                <p className="flex items-center gap-4 font-mono text-[11px] uppercase tracking-label text-mocha">
                  Where we fly <span className="h-px w-16 bg-caramel" />
                </p>
                <h2 className="mt-5 font-display text-[clamp(2.4rem,4.4vw,4.2rem)] leading-[0.98] tracking-[-0.015em] text-bitter">
                  Hillsides, marshlands, and the lines between them.
                </h2>
                <p className="mt-4 max-w-xl text-[16px] leading-relaxed text-mocha">
                  Seven kinds of place, one way of reading them. Hover a world to see it the way the drone does.
                </p>
              </Reveal>
            }
          />
        </section>

        {/* ---------- Accuracy ---------- */}
        <section id="accuracy" className="scroll-mt-16 border-t border-bitter/10 bg-cream py-24 sm:py-32">
          <div className="mx-auto grid max-w-page gap-14 px-5 sm:px-8 lg:grid-cols-2 lg:gap-20 lg:px-12">
            <Reveal>
              <p className="font-mono text-[11px] uppercase tracking-label text-mocha">Accuracy first</p>
              <h2 className="mt-5 font-display text-[clamp(3rem,6vw,5.6rem)] leading-[0.94] tracking-[-0.02em] text-bitter">
                &ldquo;Not sure&rdquo; is <span className="italic">an answer.</span>
              </h2>
              <div className="mt-8 max-w-xl space-y-5 text-[17px] leading-relaxed text-mocha">
                <p>
                  A confident wrong label is worse than no label. Farmers, agronomists and insurers act on what we show, so a crop is
                  named only when two independent looks at the plot agree.
                </p>
                <p>
                  Every field check is kept and counted. How often we are right is measured against them, and each answer carries its
                  level: low until enough fields have been checked, higher only when the record earns it.
                </p>
              </div>
              <ul className="mt-10 grid max-w-xl grid-cols-1 gap-px overflow-hidden rounded-2xl bg-bitter/10 sm:grid-cols-3">
                {[
                  ["Two looks", "agree, or we abstain"],
                  ["Field checks", "keep the score"],
                  ["Every answer", "says how sure"],
                ].map(([a, b]) => (
                  <li key={a} className="bg-cream p-5">
                    <div className="font-display text-[24px] leading-none text-bitter">{a}</div>
                    <div className="mt-2 text-[14px] text-mocha">{b}</div>
                  </li>
                ))}
              </ul>
            </Reveal>
            <Reveal delay={150} className="lg:pt-16">
              <TwoLooks />
            </Reveal>
          </div>
        </section>

        {/* ---------- How it works ---------- */}
        <section id="how" className="scroll-mt-16 bg-espresso py-24 text-sand sm:py-32">
          <div className="mx-auto grid max-w-page gap-16 px-5 sm:px-8 lg:grid-cols-[1fr_1.05fr] lg:gap-20 lg:px-12">
            <div>
              <Reveal>
                <p className="font-mono text-[11px] uppercase tracking-label text-caramel">How it works</p>
                <h2 className="mt-5 font-display text-[clamp(2.8rem,5.4vw,5rem)] leading-[0.96] tracking-[-0.015em]">
                  Fly. Read. <span className="italic text-caramel">Ask.</span>
                </h2>
              </Reveal>
              <ol className="mt-12 space-y-0">
                {[
                  ["01", "Fly", "Upload a drone flight, or we fly it with you. Satellite, rain and soil data for the same place are already waiting."],
                  ["02", "Read", "Plots outlined, crops named, plants counted, gaps found. Each answer is drawn on the photo, with how sure we are."],
                  ["03", "Ask", "Ask Sage in plain words. It knows when the drone photo answers your question and when the satellite does, and says which it used."],
                ].map(([n, h, t], k) => (
                  <Reveal as="li" key={n} delay={k * 120} className="grid grid-cols-[3.5rem_1fr] border-t border-white/10 py-7">
                    <span className="font-mono text-[12px] text-caramel">{n}</span>
                    <div>
                      <h3 className="text-[21px] font-medium">{h}</h3>
                      <p className="mt-2 max-w-md text-[15.5px] leading-relaxed text-latte">{t}</p>
                    </div>
                  </Reveal>
                ))}
              </ol>
            </div>
            <Reveal delay={150} className="lg:pt-10">
              <SageExample />
            </Reveal>
          </div>
        </section>

        {/* ---------- Contact ---------- */}
        <section id="contact" className="scroll-mt-16 overflow-hidden bg-cream">
          <div className="mx-auto grid max-w-page items-center gap-10 px-5 py-24 sm:px-8 sm:py-32 lg:grid-cols-[1.1fr_1fr] lg:px-12">
            <Reveal>
              <p className="font-mono text-[11px] uppercase tracking-label text-mocha">Noza Labs</p>
              <h2 className="mt-5 font-display text-[clamp(3rem,6vw,5.8rem)] leading-[0.94] tracking-[-0.02em] text-bitter">
                Tell us what you need to see <span className="italic">from the air.</span>
              </h2>
              <p className="mt-7 max-w-lg text-[17px] leading-relaxed text-mocha">
                Farms, cooperatives, insurers, grid and tower operators: bring a place and a question. We will fly it and answer it with
                you.
              </p>
              <div className="mt-10 flex flex-wrap items-center gap-4">
                <a
                  href={PHONES[0].href}
                  className="group inline-flex items-center gap-4 rounded-full bg-bitter py-4 pl-8 pr-7 text-[15px] font-medium text-cream transition-colors hover:bg-bark"
                >
                  Call {PHONES[0].label}
                  <Arrow className="transition-transform duration-300 group-hover:translate-x-1" />
                </a>
                <a
                  href={INGABE_URL}
                  className="inline-flex items-center gap-3 rounded-full border border-bitter/20 px-7 py-4 text-[15px] text-bitter transition-colors hover:border-bitter"
                >
                  Open Ingabe <Arrow diagonal />
                </a>
              </div>
            </Reveal>
            <Reveal delay={150} className="group">
              <WorldTile preset="hills" seed={12} className="aspect-[4/3.3] w-full" />
            </Reveal>
          </div>
        </section>
      </main>

      <footer className="bg-night text-latte">
        <div className="mx-auto max-w-page px-5 pb-10 pt-16 sm:px-8 lg:px-12">
          <div className="grid gap-12 md:grid-cols-[1.4fr_1fr_1fr_1fr]">
            <div>
              <Wordmark light />
              <p className="mt-5 max-w-xs font-mono text-[11px] uppercase leading-[1.8] tracking-[0.16em] text-latte/80">
                See it from above.
                <br />
                Answer it on the ground.
              </p>
            </div>
            <FooterCol
              title="What we read"
              links={[
                ["Crop map", "#read"],
                ["Plant counts", "#read"],
                ["Towers & lines", "#read"],
              ]}
            />
            <FooterCol
              title="Ingabe"
              links={[
                ["Open the app", INGABE_URL],
                ["Sign in", SIGN_IN_URL],
                ["How it works", "#how"],
              ]}
            />
            <div>
              <h4 className="font-mono text-[11px] uppercase tracking-label text-sand">Contact</h4>
              <ul className="mt-4 space-y-2.5 text-[14px]">
                {PHONES.map((p) => (
                  <li key={p.href}>
                    <a href={p.href} className="transition-colors hover:text-sand">
                      {p.label}
                    </a>
                  </li>
                ))}
                <li>Kigali, Rwanda</li>
              </ul>
            </div>
          </div>
          <div className="mt-16 flex flex-col justify-between gap-3 border-t border-white/10 pt-6 text-[12.5px] sm:flex-row">
            <span>© {new Date().getFullYear()} Noza Labs</span>
            <span className="font-mono uppercase tracking-[0.14em] text-latte/70">Made in Kigali</span>
          </div>
        </div>
      </footer>
    </MotionProvider>
  );
}

function Group({ label, note, cards }: { label: string; note: string; cards: Card[] }) {
  return (
    <div className="mt-16">
      <Reveal className="mb-5 flex items-center justify-between border-b border-white/10 pb-4">
        <h3 className="font-mono text-[12px] uppercase tracking-label text-sand">{label}</h3>
        <span className="font-mono text-[11px] uppercase tracking-[0.14em] text-latte/80">{note}</span>
      </Reveal>
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {cards.map((c, k) => (
          <Reveal
            as="article"
            key={c.model}
            delay={k * 90}
            className={`group relative flex min-h-[460px] flex-col overflow-hidden rounded-[22px] bg-espresso p-7 ring-1 ring-inset ring-white/[0.05] transition-[background-color,transform] duration-500 hover:-translate-y-0.5 hover:bg-chocolate ${
              c.wide ? "lg:col-span-2" : ""
            }`}
          >
            <div className="relative z-10 flex items-start justify-between gap-4">
              <div>
                <p className="font-mono text-[11px] uppercase tracking-label text-latte/80">{c.tag}</p>
                <h4 className="mt-3 text-[24px] font-medium leading-tight tracking-[-0.01em] text-sand">{c.title}</h4>
                <p className="mt-3 max-w-sm text-[14.5px] leading-relaxed text-latte">{c.text}</p>
              </div>
              <span
                className={`shrink-0 rounded-full px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.12em] ${
                  c.live ? "bg-caramel/15 text-caramel" : "border border-white/10 text-latte"
                }`}
              >
                {c.live ? "Live" : "Building"}
              </span>
            </div>
            <WireDrawing name={c.model} className="pointer-events-none -mx-4 mt-auto h-[290px] w-[calc(100%+2rem)] self-center sm:h-[310px]" />
          </Reveal>
        ))}
      </div>
    </div>
  );
}

function FooterCol({ title, links }: { title: string; links: [string, string][] }) {
  return (
    <div>
      <h4 className="font-mono text-[11px] uppercase tracking-label text-sand">{title}</h4>
      <ul className="mt-4 space-y-2.5 text-[14px]">
        {links.map(([l, h]) => (
          <li key={l}>
            <a href={h} className="transition-colors hover:text-sand">
              {l}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}
