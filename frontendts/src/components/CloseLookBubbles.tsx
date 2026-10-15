import { Marker, type Map as MLMap, Popup } from 'maplibre-gl';
import { useEffect, useMemo } from 'react';
import { useFieldCloseLook, useInspectionCloseLooks } from '@/hooks/useCloseLook';
import { isPhotoInspectionLayer } from '@/lib/inspectionPhoto';
import type { MapLayer } from '@/lib/types';

// Picture bubbles pinned to the map where a close look was taken: each tower photo at the place the drone took it
// (its GPS), each square of a farm photo at its centre. They are markers on the map itself, so they stay on their
// coordinates as the map moves, zooms or tilts. Tapping one opens what the close look saw there, said as things for
// a person to check; nothing here is a count.

const FOOTNOTE = 'A close look: not measured. A person checks before anything is counted or acted on.';

interface Bubble {
  key: string;
  lon: number;
  lat: number;
  picture: string;
  badge: string | null;
  title: string;
  lines: { label: string; text: string }[];
}

function el(tag: string, className: string, text?: string): HTMLElement {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** The pop-up fits in the room above its bubble (`room` px): the picture always shows, the words scroll below it. */
function popupContent(b: Bubble, room: number): HTMLElement {
  const box = el('div', 'close-look-pop');
  box.style.maxHeight = `${Math.max(220, room)}px`;
  const img = document.createElement('img');
  img.src = b.picture;
  img.alt = b.title;
  img.loading = 'lazy';
  box.append(img, el('div', 'close-look-pop-title', b.title));
  const list = el('ul', 'close-look-pop-lines');
  for (const line of b.lines.slice(0, 7)) {
    const li = el('li', '');
    li.append(el('b', '', `${line.label}: `), document.createTextNode(line.text));
    list.append(li);
  }
  box.append(list, el('p', 'close-look-pop-note', FOOTNOTE));
  return box;
}

/** Pins the bubbles on the map for as long as they are given; one popup open at a time. */
function usePinned(map: MLMap | null, bubbles: Bubble[]) {
  useEffect(() => {
    if (!map || bubbles.length === 0) return;
    // Opens above its bubble; the map first slides the bubble low on the screen, so the pop-up has room above it and
    // stays clear of the chat box and the panels.
    const popup = new Popup({ className: 'close-look-popup', closeButton: true, maxWidth: '320px', offset: 58, anchor: 'bottom' });
    const markers = bubbles.map((b) => {
      const node = el('button', 'close-look-bubble');
      node.setAttribute('type', 'button');
      node.setAttribute('aria-label', `${b.title}: open what the close look saw`);
      const img = document.createElement('img');
      img.src = b.picture;
      img.alt = '';
      img.loading = 'lazy';
      node.append(img);
      if (b.badge) node.append(el('span', 'close-look-badge', b.badge));
      node.addEventListener('click', (e) => {
        e.stopPropagation();
        const height = map.getContainer().clientHeight;
        map.easeTo({ center: [b.lon, b.lat], offset: [0, Math.round(height * 0.3)], duration: 400 });
        // The bubble ends 80% down the map; above it go the pop-up's offset, its padding and a margin.
        popup
          .setLngLat([b.lon, b.lat])
          .setDOMContent(popupContent(b, Math.round(height * 0.8) - 110))
          .addTo(map);
      });
      return new Marker({ element: node, anchor: 'bottom' }).setLngLat([b.lon, b.lat]).addTo(map);
    });
    return () => {
      popup.remove();
      for (const m of markers) m.remove();
    };
  }, [map, bubbles]);
}

const WORRY: Record<string, string> = {
  structure: 'Steel',
  theft_tampering: 'Site',
  alignment: 'Antennas',
  equipment: 'Equipment',
  damage: 'Damage',
  hot_spots: 'Heat',
  obstruction_light: 'Light',
  surroundings: 'Around',
};

function InspectionBubbles({ map, layerId }: { map: MLMap | null; layerId: string }) {
  const looks = useInspectionCloseLooks(layerId);
  const bubbles = useMemo<Bubble[]>(
    () =>
      (looks.data?.photos ?? []).map((p) => ({
        key: p.sha256,
        lon: p.lon,
        lat: p.lat,
        picture: p.url,
        badge: p.flags.length ? String(p.flags.length) : null,
        title: `${p.filename} · where the drone took it`,
        lines: [
          ...p.flags.map((f) => ({ label: 'Check', text: f })),
          ...p.seen.filter((w) => w.id === 'structure' || w.id === 'damage').map((w) => ({ label: WORRY[w.id], text: w.answer })),
        ],
      })),
    [looks.data],
  );
  usePinned(map, bubbles);
  return null;
}

function FieldBubbles({ map, layerId }: { map: MLMap | null; layerId: string }) {
  const look = useFieldCloseLook(layerId);
  const bubbles = useMemo<Bubble[]>(() => {
    const result = look.data?.result;
    if (!result) return [];
    return result.squares.map((s) => ({
      key: String(s.n),
      lon: s.lon,
      lat: s.lat,
      picture: s.picture,
      badge: String(s.n),
      title: `${result.place} · square ${s.n} (40 m)`,
      lines: [
        ...s.crops.map((c) => ({ label: c.confidence === 'low' ? 'Maybe' : 'Crop', text: `${c.crop}${c.stage ? `, ${c.stage}` : ''}` })),
        { label: 'Bare soil', text: s.bare_or_gaps },
        { label: 'Weeds', text: s.weeds },
        { label: 'Stress', text: s.stress_or_disease },
        ...(s.odd ? [{ label: 'Odd', text: s.odd }] : []),
      ].filter((l) => l.text),
    }));
  }, [look.data]);
  usePinned(map, bubbles);
  return null;
}

/** Close-look bubbles for the inspections and farm photos shown on the map. */
export function CloseLookBubbles({ map, layers, hiddenLayerIDs }: { map: MLMap | null; layers: MapLayer[]; hiddenLayerIDs: string[] }) {
  const inspections = useMemo(
    () => layers.filter((l) => isPhotoInspectionLayer(l) && !hiddenLayerIDs.includes(l.id)),
    [layers, hiddenLayerIDs],
  );
  const photos = useMemo(() => layers.filter((l) => l.type === 'raster' && !hiddenLayerIDs.includes(l.id)), [layers, hiddenLayerIDs]);
  return (
    <>
      {inspections.map((l) => (
        <InspectionBubbles key={l.id} map={map} layerId={l.id} />
      ))}
      {photos.map((l) => (
        <FieldBubbles key={l.id} map={map} layerId={l.id} />
      ))}
    </>
  );
}
