import { LocaleProvider } from "@/components/Locale";
import { HeroWorld, WorldTile } from "@/components/MiniWorld";
import { MotionProvider, PauseButton } from "@/components/MotionProvider";
import { Arrow, IngabeIcon, Nav, NozaN, Wordmark } from "@/components/Nav";
import { Parallax } from "@/components/Parallax";
import { Reveal } from "@/components/Reveal";
import { SageExample } from "@/components/SageExample";
import { TwoLooks } from "@/components/TwoLooks";
import { WireDrawing } from "@/components/WireDrawing";
import { Words } from "@/components/Words";
import { WorldsStrip } from "@/components/WorldsStrip";
import { DICTS, type Dict, type Locale } from "@/lib/i18n";
import { INGABE_URL, PHONES, REQUEST_ACCESS_URL, SIGN_IN_URL } from "@/lib/site";
import type { ModelName } from "@/lib/wire3d";

type CardKey = keyof Dict["read"]["cards"];
type Card = { model: ModelName & CardKey; live: boolean; wide?: boolean };

const FARMS: Card[] = [
  { model: "crops", live: true },
  { model: "count", live: true },
  { model: "gaps", live: true },
];

const LINES: Card[] = [
  { model: "tower", live: false },
  { model: "vegetation", live: false },
  { model: "corridor", live: false },
];

/** The whole Noza Labs page in one language. The same layout serves "/" (English) and "/fr" (French). */
export function Home({ locale }: { locale: Locale }) {
  const t = DICTS[locale];
  return (
    <LocaleProvider locale={locale}>
      <MotionProvider>
        <Nav />
        <main id="top">
          {/* ---------- Hero ---------- */}
          <section className="relative overflow-hidden pt-16">
            <div className="relative mx-auto max-w-page px-5 sm:px-8 lg:flex lg:min-h-[calc(100vh-64px-146px)] lg:items-center lg:px-12">
              <div className="relative z-10 pt-10 lg:max-w-[36rem] lg:pb-16 lg:pt-0">
                <p className="arrive font-mono text-[12px] uppercase tracking-label text-mocha">{t.hero.kicker}</p>
                <h1 className="mt-6 font-display text-[clamp(3rem,6.4vw,6.4rem)] leading-[0.93] tracking-[-0.02em] text-bitter">
                  <Words text={t.hero.title} start={150} />
                </h1>
                <p className="arrive mt-7 max-w-[28rem] text-[18px] leading-relaxed text-mocha sm:text-[19px]" style={{ animationDelay: "800ms" }}>
                  {t.hero.lead}
                </p>
                <div className="arrive mt-9 flex flex-wrap items-center gap-6" style={{ animationDelay: "1000ms" }}>
                  <a
                    href="#contact"
                    className="group inline-flex items-center gap-4 rounded-full bg-bitter py-4 pl-8 pr-7 text-[15px] font-medium text-cream transition-colors hover:bg-bark"
                  >
                    {t.hero.talk}
                    <Arrow className="transition-transform duration-300 group-hover:translate-x-1" />
                  </a>
                  <a href={INGABE_URL} className="link-line inline-flex items-center gap-2 pb-0.5 text-[15px] text-bitter">
                    {t.nav.openIngabe} <Arrow diagonal />
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
                {t.hero.motto[0]}
                <br />
                {t.hero.motto[1]}
              </p>
            </div>
            <div className="border-t border-bitter/10">
              <dl className="mx-auto grid max-w-page grid-cols-2 px-5 sm:px-8 lg:grid-cols-4 lg:px-12">
                {t.facts.map((f, k) => (
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
                  <p className="font-mono text-[11px] uppercase tracking-label text-caramel">{t.read.kicker}</p>
                  <h2 className="mt-5 font-display text-[clamp(2.6rem,5vw,4.6rem)] leading-[0.98] tracking-[-0.015em]">{t.read.title}</h2>
                  <p className="mt-5 max-w-xl text-[17px] leading-relaxed text-latte">{t.read.lead}</p>
                </Reveal>
                <PauseButton className="self-start text-latte lg:self-end" />
              </div>

              <Group label={t.read.farms.label} note={t.read.farms.note} cards={FARMS} t={t} />
              <Group label={t.read.lines.label} note={t.read.lines.note} cards={LINES} t={t} />

              <Reveal className="mt-12">
                <a href="#contact" className="group inline-flex items-center gap-3 text-[15px] text-sand">
                  <span className="link-line pb-0.5">{t.read.cta}</span>
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
                    {t.where.kicker} <span className="h-px w-16 bg-caramel" />
                  </p>
                  <h2 className="mt-5 font-display text-[clamp(2.4rem,4.4vw,4.2rem)] leading-[0.98] tracking-[-0.015em] text-bitter">{t.where.title}</h2>
                  <p className="mt-4 max-w-xl text-[16px] leading-relaxed text-mocha">{t.where.lead}</p>
                </Reveal>
              }
            />
          </section>

          {/* ---------- Accuracy ---------- */}
          <section id="accuracy" className="scroll-mt-16 border-t border-bitter/10 bg-cream py-24 sm:py-32">
            <div className="mx-auto grid max-w-page gap-14 px-5 sm:px-8 lg:grid-cols-2 lg:gap-20 lg:px-12">
              <Reveal>
                <p className="font-mono text-[11px] uppercase tracking-label text-mocha">{t.accuracy.kicker}</p>
                <h2 className="mt-5 font-display text-[clamp(3rem,6vw,5.6rem)] leading-[0.94] tracking-[-0.02em] text-bitter">
                  {t.accuracy.titleStart} <span className="italic">{t.accuracy.titleItalic}</span>
                </h2>
                <div className="mt-8 max-w-xl space-y-5 text-[17px] leading-relaxed text-mocha">
                  <p>{t.accuracy.p1}</p>
                  <p>{t.accuracy.p2}</p>
                </div>
                <ul className="mt-10 grid max-w-xl grid-cols-1 gap-px overflow-hidden rounded-2xl bg-bitter/10 sm:grid-cols-3">
                  {t.accuracy.tiles.map(([a, b]) => (
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
                  <p className="font-mono text-[11px] uppercase tracking-label text-caramel">{t.how.kicker}</p>
                  <h2 className="mt-5 font-display text-[clamp(2.8rem,5.4vw,5rem)] leading-[0.96] tracking-[-0.015em]">
                    {t.how.titleStart} <span className="italic text-caramel">{t.how.titleItalic}</span>
                  </h2>
                </Reveal>
                <ol className="mt-12 space-y-0">
                  {t.how.steps.map(([n, h, text], k) => (
                    <Reveal as="li" key={n} delay={k * 120} className="grid grid-cols-[3.5rem_1fr] border-t border-white/10 py-7">
                      <span className="font-mono text-[12px] text-caramel">{n}</span>
                      <div>
                        <h3 className="text-[21px] font-medium">{h}</h3>
                        <p className="mt-2 max-w-md text-[15.5px] leading-relaxed text-latte">{text}</p>
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

          {/* ---------- Access: invite-only, how people are admitted and how sign-in works ---------- */}
          <section id="access" className="scroll-mt-16 border-t border-bitter/10 bg-cream py-24 sm:py-32">
            <div className="mx-auto grid max-w-page gap-16 px-5 sm:px-8 lg:grid-cols-[1fr_1fr] lg:gap-20 lg:px-12">
              <div>
                <Reveal>
                  <p className="font-mono text-[11px] uppercase tracking-label text-mocha">{t.access.kicker}</p>
                  <h2 className="mt-5 font-display text-[clamp(2.8rem,5.4vw,5rem)] leading-[0.96] tracking-[-0.015em] text-bitter">
                    {t.access.titleStart} <span className="italic">{t.access.titleItalic}</span>
                  </h2>
                  <p className="mt-7 max-w-lg text-[17px] leading-relaxed text-mocha">{t.access.lead}</p>
                </Reveal>
                <ol className="mt-12">
                  {t.access.steps.map(([n, h, text], k) => (
                    <Reveal as="li" key={n} delay={k * 100} className="grid grid-cols-[3.5rem_1fr] border-t border-bitter/10 py-6">
                      <span className="font-mono text-[12px] text-bark">{n}</span>
                      <div>
                        <h3 className="text-[19px] font-medium text-bitter">{h}</h3>
                        <p className="mt-1.5 max-w-md text-[15.5px] leading-relaxed text-mocha">{text}</p>
                      </div>
                    </Reveal>
                  ))}
                </ol>
                <div className="mt-10 flex flex-wrap items-center gap-x-6 gap-y-4">
                  <a
                    href={REQUEST_ACCESS_URL}
                    className="group inline-flex items-center gap-4 rounded-full bg-bitter py-4 pl-8 pr-7 text-[15px] font-medium text-cream transition-colors hover:bg-bark"
                  >
                    {t.access.request}
                    <Arrow className="transition-transform duration-300 group-hover:translate-x-1" />
                  </a>
                  <a href={SIGN_IN_URL} className="link-line pb-0.5 text-[15px] text-bitter">
                    {t.access.already}
                  </a>
                </div>
              </div>

              <Reveal delay={150} className="lg:pt-24">
                <div className="rounded-2xl border border-bitter/10 bg-sand p-7 sm:p-9">
                  <h3 className="text-[19px] font-medium text-bitter">{t.access.boxTitle}</h3>
                  <ul className="mt-6 space-y-5 text-[15.5px] leading-relaxed text-mocha">
                    {t.access.points.map(([head, text]) => (
                      <li key={head}>
                        <span className="font-medium text-bitter">{head}</span> {text}
                      </li>
                    ))}
                  </ul>
                  <p className="mt-7 border-t border-bitter/10 pt-6 text-[14px] leading-relaxed text-mocha">
                    {t.access.waiting}{" "}
                    <a href={PHONES[0].href} className="link-line text-bitter">
                      {PHONES[0].label}
                    </a>
                    .
                  </p>
                </div>
              </Reveal>
            </div>
          </section>

          {/* ---------- Contact ---------- */}
          <section id="contact" className="scroll-mt-16 overflow-hidden bg-cream">
            <div className="mx-auto grid max-w-page items-center gap-10 px-5 py-24 sm:px-8 sm:py-32 lg:grid-cols-[1.1fr_1fr] lg:px-12">
              <Reveal>
                <p className="font-mono text-[11px] uppercase tracking-label text-mocha">{t.contact.kicker}</p>
                <h2 className="mt-5 font-display text-[clamp(3rem,6vw,5.8rem)] leading-[0.94] tracking-[-0.02em] text-bitter">
                  {t.contact.titleStart} <span className="italic">{t.contact.titleItalic}</span>
                </h2>
                <p className="mt-7 max-w-lg text-[17px] leading-relaxed text-mocha">{t.contact.lead}</p>
                <div className="mt-10 flex flex-wrap items-center gap-4">
                  <a
                    href={PHONES[0].href}
                    className="group inline-flex items-center gap-4 rounded-full bg-bitter py-4 pl-8 pr-7 text-[15px] font-medium text-cream transition-colors hover:bg-bark"
                  >
                    {t.contact.call} {PHONES[0].label}
                    <Arrow className="transition-transform duration-300 group-hover:translate-x-1" />
                  </a>
                  <a
                    href={INGABE_URL}
                    className="inline-flex items-center gap-3 rounded-full border border-bitter/20 py-3.5 pl-4 pr-7 text-[15px] text-bitter transition-colors hover:border-bitter"
                  >
                    <IngabeIcon className="h-7 w-7" />
                    {t.nav.openIngabe} <Arrow diagonal />
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
                  {t.hero.motto[0]}
                  <br />
                  {t.hero.motto[1]}
                </p>
                <div className="mt-10 flex max-w-xs items-start gap-4">
                  <svg viewBox="12 14 40 36" className="h-10 w-11 shrink-0" aria-hidden>
                    <NozaN color="#F3EDE6" />
                  </svg>
                  <p className="text-[13px] leading-relaxed text-latte">{t.footer.mark}</p>
                </div>
              </div>
              <FooterCol
                title={t.footer.readTitle}
                links={[
                  [t.footer.readLinks[0], "#read"],
                  [t.footer.readLinks[1], "#read"],
                  [t.footer.readLinks[2], "#read"],
                ]}
              />
              <FooterCol
                title={t.footer.appTitle}
                links={[
                  [t.footer.appLinks[0], INGABE_URL],
                  [t.footer.appLinks[1], SIGN_IN_URL],
                  [t.footer.appLinks[2], REQUEST_ACCESS_URL],
                  [t.footer.appLinks[3], "#how"],
                  [t.footer.appLinks[4], "#access"],
                ]}
              />
              <div>
                <h4 className="font-mono text-[11px] uppercase tracking-label text-sand">{t.footer.contact}</h4>
                <ul className="mt-4 space-y-2.5 text-[14px]">
                  {PHONES.map((p) => (
                    <li key={p.href}>
                      <a href={p.href} className="transition-colors hover:text-sand">
                        {p.label}
                      </a>
                    </li>
                  ))}
                  <li>{t.footer.place}</li>
                </ul>
              </div>
            </div>
            <div className="mt-16 flex flex-col justify-between gap-3 border-t border-white/10 pt-6 text-[12.5px] sm:flex-row">
              <span>© {new Date().getFullYear()} Noza Labs</span>
              <span className="font-mono uppercase tracking-[0.14em] text-latte/70">{t.footer.made}</span>
            </div>
          </div>
        </footer>
      </MotionProvider>
    </LocaleProvider>
  );
}

function Group({ label, note, cards, t }: { label: string; note: string; cards: Card[]; t: Dict }) {
  return (
    <div className="mt-16">
      <Reveal className="mb-5 flex items-center justify-between border-b border-white/10 pb-4">
        <h3 className="font-mono text-[12px] uppercase tracking-label text-sand">{label}</h3>
        <span className="font-mono text-[11px] uppercase tracking-[0.14em] text-latte/80">{note}</span>
      </Reveal>
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {cards.map((c, k) => {
          const text = t.read.cards[c.model];
          return (
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
                  <p className="font-mono text-[11px] uppercase tracking-label text-latte/80">{text.tag}</p>
                  <h4 className="mt-3 text-[24px] font-medium leading-tight tracking-[-0.01em] text-sand">{text.title}</h4>
                  <p className="mt-3 max-w-sm text-[14.5px] leading-relaxed text-latte">{text.text}</p>
                </div>
                <span
                  className={`shrink-0 rounded-full px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.12em] ${
                    c.live ? "bg-caramel/15 text-caramel" : "border border-white/10 text-latte"
                  }`}
                >
                  {c.live ? t.read.live : t.read.building}
                </span>
              </div>
              <WireDrawing name={c.model} className="pointer-events-none -mx-4 mt-auto h-[290px] w-[calc(100%+2rem)] self-center sm:h-[310px]" />
            </Reveal>
          );
        })}
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
