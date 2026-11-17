import { CircleHelp, Layers3, MessageCircle, Pentagon, X } from 'lucide-react';
import { createContext, type ReactNode, useCallback, useContext, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';

// One drawer for the map's tools, so nothing lies on the photo.
//
// Before: the layer list, the question cards, the field panel and Sage each placed themselves over the map by
// their own rule, and "Hide panels" put away only some of them. Here one column beside the map holds them all,
// one at a time; the map takes the room that is left and is never covered. Closing the drawer is "hide panels".
//
// Behind a switch on this device (off unless asked for): open any map with ?drawer=1 to turn it on and
// ?drawer=0 to turn it off. Off, every tool is placed exactly as before.

export type Tool = 'layers' | 'questions' | 'fields' | 'sage';

const SWITCH_KEY = 'ingabe.oneDrawer';
const TOOL_KEY = 'ingabe.oneDrawer.tool';
const TOOLS: { id: Tool; label: string; icon: typeof Layers3; nothing: string }[] = [
  { id: 'layers', label: 'Layers', icon: Layers3, nothing: 'No layers on this map yet.' },
  { id: 'questions', label: 'Questions', icon: CircleHelp, nothing: 'Questions show here for a drone photo or a tower inspection.' },
  { id: 'fields', label: 'Fields', icon: Pentagon, nothing: 'Outlining fields is not available for this photo.' },
  { id: 'sage', label: 'Sage', icon: MessageCircle, nothing: '' },
];

/** Whether this device asked for the one drawer. `?drawer=1` / `?drawer=0` in the address sets and keeps it. */
export function oneDrawerWanted(): boolean {
  try {
    const asked = new URLSearchParams(window.location.search).get('drawer');
    if (asked === '1' || asked === '0') localStorage.setItem(SWITCH_KEY, asked);
    return localStorage.getItem(SWITCH_KEY) === '1';
  } catch {
    // private window or blocked storage: the address alone decides, for this visit
    return new URLSearchParams(window.location.search).get('drawer') === '1';
  }
}

interface Drawer {
  on: boolean;
  /** The tool the drawer shows; null: the drawer is closed and the map has the whole width. */
  active: Tool | null;
  open: (tool: Tool) => void;
  toggle: (tool: Tool) => void;
  close: () => void;
  slots: Partial<Record<Tool, HTMLElement>>;
  setSlot: (tool: Tool, element: HTMLElement | null) => void;
}

const DrawerContext = createContext<Drawer | null>(null);

/** true inside the drawer: a tool then fills the drawer's width in the page's flow and does not place itself. */
export function useInDrawer(): boolean {
  return useContext(DrawerContext)?.on ?? false;
}

// Read once, as the app loads: the router may rewrite the address (dropping ?drawer=1) before a map is shown.
const WANTED = oneDrawerWanted();

export function useMapDrawer(): Drawer {
  const on = WANTED;
  const [active, setActiveState] = useState<Tool | null>(() => {
    try {
      const kept = localStorage.getItem(TOOL_KEY);
      // the first time, the layers show, as they always did; "none" is a drawer the reader closed
      return kept === 'none' ? null : (TOOLS.find((t) => t.id === kept)?.id ?? 'layers');
    } catch {
      return 'layers';
    }
  });
  const [slots, setSlots] = useState<Partial<Record<Tool, HTMLElement>>>({});
  const setActive = useCallback((tool: Tool | null) => {
    setActiveState(tool);
    try {
      localStorage.setItem(TOOL_KEY, tool ?? 'none');
    } catch {
      // private window or blocked storage: the choice lasts for this visit only
    }
  }, []);
  // A slot is registered once and kept: React hands a ref callback null and then the element again on every
  // render, and answering the null by forgetting the slot would render again, without end.
  const setSlot = useCallback((tool: Tool, element: HTMLElement | null) => {
    if (!element) return;
    setSlots((current) => (current[tool] === element ? current : { ...current, [tool]: element }));
  }, []);
  const open = useCallback((tool: Tool) => setActive(tool), [setActive]);
  const close = useCallback(() => setActive(null), [setActive]);
  const toggle = useCallback(
    (tool: Tool) =>
      setActiveState((now) => {
        const next = now === tool ? null : tool;
        try {
          localStorage.setItem(TOOL_KEY, next ?? 'none');
        } catch {
          // as above
        }
        return next;
      }),
    [],
  );
  return useMemo(() => ({ on, active, open, toggle, close, slots, setSlot }), [on, active, open, toggle, close, slots, setSlot]);
}

export function MapDrawerProvider({ drawer, children }: { drawer: Drawer; children: ReactNode }) {
  return <DrawerContext.Provider value={drawer}>{children}</DrawerContext.Provider>;
}

/** A tool of the map. Drawer on: its content is shown in the drawer under `tool` (kept mounted while another
 * tool shows, so nothing typed or opened is lost). Drawer off: the content is rendered where it stands. */
export function ToolSlot({ tool, children }: { tool: Tool; children: ReactNode }) {
  const drawer = useContext(DrawerContext);
  if (!drawer?.on) return <>{children}</>;
  const slot = drawer.slots[tool];
  return slot ? createPortal(children, slot) : null;
}

const tab = 'flex size-11 cursor-pointer flex-col items-center justify-center gap-0.5 rounded-[12px] text-[10px] font-semibold';

/** The rail of tools and the one drawer beside the map. Wide screens: a column left of the map. Phones: a bar of
 * the same tools along the bottom and the drawer as a sheet above it, at most half the screen. */
export function MapDrawer({ drawer }: { drawer: Drawer }) {
  const shown = TOOLS.find((t) => t.id === drawer.active) ?? null;
  return (
    <div
      className="z-40 flex max-sm:fixed max-sm:inset-x-0 max-sm:bottom-0 max-sm:flex-col-reverse sm:h-screen sm:shrink-0 sm:flex-row"
      data-testid="map-drawer"
    >
      <nav
        aria-label="Map tools"
        className="flex shrink-0 items-center gap-1 border-line bg-panel p-1.5 max-sm:justify-around max-sm:border-t sm:flex-col sm:border-r"
      >
        {TOOLS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            className={`${tab} ${drawer.active === id ? 'bg-caramel/20 text-caramel' : 'text-fg-soft hover:bg-white/[0.06] hover:text-fg'}`}
            aria-pressed={drawer.active === id}
            title={drawer.active === id ? `Close ${label}` : label}
            onClick={() => drawer.toggle(id)}
          >
            <Icon className="size-5" aria-hidden="true" />
            {label}
          </button>
        ))}
      </nav>
      <section
        aria-label={shown ? shown.label : 'Map tools'}
        className={`${shown ? 'flex' : 'hidden'} min-h-0 flex-col border-line bg-canvas text-fg max-sm:max-h-[50vh] max-sm:rounded-t-[18px] max-sm:border-t sm:w-[380px] sm:border-r`}
      >
        <div className="flex shrink-0 items-center justify-between px-4 pt-3 pb-1">
          <h2 className="m-0 text-[15px] font-semibold">{shown?.label}</h2>
          <button
            type="button"
            className="flex size-9 cursor-pointer items-center justify-center rounded-full text-fg-muted hover:bg-white/10 hover:text-fg"
            aria-label="Close the drawer"
            title="Close, to see the whole map"
            onClick={drawer.close}
          >
            <X className="size-4" aria-hidden="true" />
          </button>
        </div>
        {TOOLS.map(({ id, nothing }) => (
          <div
            key={id}
            ref={(element) => drawer.setSlot(id, element)}
            data-tool={id}
            data-nothing={nothing}
            className={`${drawer.active === id ? 'flex' : 'hidden'} min-h-0 grow flex-col gap-3 overflow-y-auto px-3 pb-3 empty:after:px-1 empty:after:text-[13px] empty:after:text-fg-muted empty:after:content-[attr(data-nothing)]`}
          />
        ))}
      </section>
    </div>
  );
}
