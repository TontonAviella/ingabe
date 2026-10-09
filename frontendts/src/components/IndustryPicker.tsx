import '@fontsource/instrument-serif/400.css';
import '@fontsource/instrument-serif/400-italic.css';
import { apiFetch } from '@mundi/ee';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { IngabeIcon } from '@/components/Brand';
import { INDUSTRY_KEY, type IndustryKey, type IndustryState } from '@/components/IndustryGate';
import { buildScene, type Preset, renderWorld } from '@/lib/miniworld';

// The industry picker itself, loaded only when it is shown (it carries the miniature worlds' drawing code).

const WORLDS: Record<IndustryKey, { preset: Preset; seed: number; eyebrow: string; covers: string[]; alt: string }> = {
  agriculture: {
    preset: 'hills',
    seed: 3,
    eyebrow: 'Farms',
    covers: ['Hillside plots, marshland rice, tea and irrigation schemes', 'Crops, bare ground and plant health'],
    alt: 'A hillside of terraced farm plots',
  },
  power_grid: {
    preset: 'corridor',
    seed: 8,
    eyebrow: 'Lines',
    covers: ['High-voltage towers and village poles', 'Spans, wires and the corridor around them'],
    alt: 'A power line corridor: lattice towers carrying wires across hills',
  },
  telecom: {
    preset: 'telecom',
    seed: 2,
    eyebrow: 'Masts',
    covers: ['Telecom and monitoring masts', 'Antennas, equipment shelters and site surroundings'],
    alt: 'A telecom mast with its equipment shelter on a hilltop site',
  },
};

/** One industry's miniature world: the land as it looks; on hover or when chosen, the drone's reading of it. */
function WorldPicture({ preset, seed, reading, alt }: { preset: Preset; seed: number; reading: boolean; alt: string }) {
  const solidRef = useRef<HTMLCanvasElement>(null);
  const wireRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const a = solidRef.current;
    const b = wireRef.current;
    if (!a || !b) return;
    const scene = buildScene(preset, seed);
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const paint = () => {
      const r = a.getBoundingClientRect();
      if (r.width < 10) return;
      const world = renderWorld(scene, r.width, r.height, dpr, 0.05);
      for (const [canvas, source] of [
        [a, world.solid],
        [b, world.wire],
      ] as const) {
        canvas.width = source.width;
        canvas.height = source.height;
        canvas.getContext('2d')?.drawImage(source, 0, 0);
      }
    };
    const observer = new ResizeObserver(paint);
    observer.observe(a);
    return () => observer.disconnect();
  }, [preset, seed]);

  return (
    <div className="relative aspect-[4/3.1] w-full" role="img" aria-label={alt}>
      <canvas
        ref={solidRef}
        className={`absolute inset-0 h-full w-full transition-opacity duration-700 ${reading ? 'opacity-0' : 'opacity-100'}`}
      />
      <canvas
        ref={wireRef}
        className={`absolute inset-0 h-full w-full transition-opacity duration-700 ${reading ? 'opacity-100' : 'opacity-0'}`}
      />
    </div>
  );
}

/** The full-screen choice of industry; onClose runs once it has faded out (after saving, or "Keep …"). */
export default function IndustryPicker({ data, onClose }: { data: IndustryState; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [chosen, setChosen] = useState<IndustryKey | null>(data.industry);
  const [hovered, setHovered] = useState<IndustryKey | null>(null);
  const [shown, setShown] = useState(false);
  const [entered, setEntered] = useState(false); // after the entrance, cards answer clicks quickly

  useEffect(() => {
    const id = requestAnimationFrame(() => setShown(true));
    const done = window.setTimeout(() => setEntered(true), 1200);
    return () => {
      cancelAnimationFrame(id);
      window.clearTimeout(done);
    };
  }, []);

  const save = useMutation({
    mutationFn: async (industry: IndustryKey) => {
      const res = await apiFetch('/api/user/industry', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ industry }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail ?? 'Could not save your choice');
      return (await res.json()) as IndustryState;
    },
    onSuccess: (saved) => {
      setShown(false);
      // Let the fade finish before the gate leaves the page.
      window.setTimeout(() => {
        queryClient.setQueryData(INDUSTRY_KEY, saved);
        onClose();
      }, 450);
    },
  });

  const canClose = data.industry !== null;
  const label = (key: IndustryKey) => data.options.find((o) => o.key === key)?.label ?? key;
  const choose = (key: IndustryKey) => setChosen(key);

  return (
    <div
      className={`fixed left-0 top-0 z-[10000] h-[100dvh] w-[100vw] overflow-y-auto overflow-x-hidden bg-[#0B0908] text-[#F6F1EB] transition-opacity duration-500 motion-reduce:transition-none ${
        shown ? 'opacity-100' : 'opacity-0'
      }`}
      role="dialog"
      aria-modal="true"
      aria-labelledby="industry-title"
    >
      {/* Low warm light from the top, like the website's night bands. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-0 h-[60vh] bg-[radial-gradient(ellipse_at_top,rgba(217,160,102,0.14),transparent_65%)]"
      />
      <div className="relative mx-auto flex min-h-full max-w-[86rem] flex-col px-5 pb-10 pt-8 sm:px-8 lg:px-12">
        <header className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <IngabeIcon className="h-9 w-9" />
            <span className="text-[15px] font-semibold tracking-[-0.03em]">ingabe</span>
            <span className="hidden font-mono text-[10px] uppercase tracking-[0.22em] text-[#B8A99B] sm:inline">
              by noza<span className="text-[#D9A066]">·</span>labs
            </span>
          </div>
          {canClose && (
            <button
              type="button"
              onClick={() => {
                setShown(false);
                window.setTimeout(onClose, 450);
              }}
              className="rounded-full border border-[#4A3326] px-4 py-1.5 text-[13px] text-[#B8A99B] transition-colors hover:border-[#D9A066] hover:text-[#F6F1EB]"
            >
              Keep {label(data.industry as IndustryKey)}
            </button>
          )}
        </header>

        <div
          className={`mt-12 max-w-3xl transition-all duration-700 motion-reduce:transition-none sm:mt-16 ${
            shown ? 'translate-y-0 opacity-100' : 'translate-y-3 opacity-0'
          }`}
        >
          <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-[#D9A066]">
            {canClose ? 'Your industry' : 'Welcome to Ingabe'}
          </p>
          <h1
            id="industry-title"
            className="mt-4 text-[clamp(2.4rem,5.2vw,4.4rem)] leading-[1.02] tracking-[-0.01em]"
            style={{ fontFamily: '"Instrument Serif", Georgia, serif' }}
          >
            Which world do <em className="text-[#D9A066]">you</em> work in?
          </h1>
          <p className="mt-5 max-w-xl text-[15px] leading-relaxed text-[#B8A99B]">
            Ingabe reads drone and satellite pictures for three industries. Choose yours and Ingabe opens with what matters to it. You can
            change this at any time.
          </p>
        </div>

        <div className="mt-10 grid flex-1 grid-cols-1 gap-5 md:grid-cols-3 lg:gap-7" role="radiogroup" aria-label="Industry">
          {(Object.keys(WORLDS) as IndustryKey[]).map((key, k) => {
            const world = WORLDS[key];
            const option = data.options.find((o) => o.key === key);
            const selected = chosen === key;
            return (
              <button
                key={key}
                type="button"
                role="radio"
                aria-checked={selected}
                onClick={() => choose(key)}
                onDoubleClick={() => save.mutate(key)}
                onMouseEnter={() => setHovered(key)}
                onMouseLeave={() => setHovered(null)}
                onFocus={() => setHovered(key)}
                onBlur={() => setHovered(null)}
                style={{ transitionDelay: shown && !entered ? `${120 + k * 90}ms` : '0ms' }}
                className={`group relative flex flex-col overflow-hidden rounded-2xl border text-left outline-none transition-all ${entered ? 'duration-200' : 'duration-700'} motion-reduce:transition-none focus-visible:ring-2 focus-visible:ring-[#D9A066] ${
                  shown ? 'translate-y-0 opacity-100' : 'translate-y-6 opacity-0'
                } ${
                  selected
                    ? 'border-[#D9A066] bg-[#17110E] shadow-[0_0_0_1px_#D9A066,0_30px_80px_-30px_rgba(217,160,102,0.45)]'
                    : 'border-[#221813] bg-[#120D0B] hover:border-[#4A3326]'
                }`}
              >
                <div className="relative bg-[#F3EDE6]">
                  <WorldPicture preset={world.preset} seed={world.seed} reading={hovered === key || selected} alt={world.alt} />
                  <span className="absolute left-3 top-3 rounded-full bg-[#0B0908]/80 px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.22em] text-[#D9A066]">
                    {hovered === key || selected ? 'As Ingabe reads it' : world.eyebrow}
                  </span>
                  <span
                    aria-hidden
                    className={`absolute right-3 top-3 flex h-6 w-6 items-center justify-center rounded-full border transition-colors ${
                      selected ? 'border-[#D9A066] bg-[#D9A066]' : 'border-[#4A3326] bg-[#0B0908]/70'
                    }`}
                  >
                    {selected && (
                      <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="#0B0908" strokeWidth="2.2">
                        <path d="M3.5 8.5l3 3 6-7" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                    )}
                  </span>
                </div>
                <div className="flex flex-1 flex-col px-5 pb-6 pt-5">
                  <span className="text-[1.9rem] leading-none" style={{ fontFamily: '"Instrument Serif", Georgia, serif' }}>
                    {option?.label ?? key}
                  </span>
                  <span className="mt-2 text-[13px] text-[#D9A066]">{option?.note}</span>
                  <ul className="mt-4 space-y-1.5 text-[13px] leading-snug text-[#B8A99B]">
                    {world.covers.map((line) => (
                      <li key={line} className="flex gap-2">
                        <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-[#6B5A4E]" />
                        {line}
                      </li>
                    ))}
                  </ul>
                </div>
              </button>
            );
          })}
        </div>

        <footer className="sticky bottom-0 -mx-5 mt-8 flex flex-col items-center gap-3 bg-gradient-to-t from-[#0B0908] via-[#0B0908] to-transparent px-5 pb-2 pt-6 sm:-mx-8 sm:flex-row sm:justify-between sm:px-8 lg:-mx-12 lg:px-12">
          <p className="text-center text-[12px] text-[#6B5A4E] sm:text-left">
            {save.isError ? (
              <span className="text-[#E9A27A]">{(save.error as Error).message}</span>
            ) : chosen ? (
              <>
                Hover a world to see how Ingabe reads it. <span className="text-[#B8A99B]">Double-click to choose at once.</span>
              </>
            ) : (
              'Pick the picture that looks like your work.'
            )}
          </p>
          <button
            type="button"
            disabled={!chosen || save.isPending}
            onClick={() => chosen && save.mutate(chosen)}
            className="group inline-flex items-center gap-2 rounded-full bg-[#D9A066] px-6 py-3 text-[14px] font-medium text-[#0B0908] transition-all hover:bg-[#E6B27D] disabled:cursor-not-allowed disabled:bg-[#2A201A] disabled:text-[#6B5A4E]"
          >
            {save.isPending ? 'Opening Ingabe…' : chosen ? `Continue with ${label(chosen)}` : 'Choose your industry'}
            <span aria-hidden className="transition-transform group-enabled:group-hover:translate-x-0.5">
              →
            </span>
          </button>
        </footer>
      </div>
    </div>
  );
}
