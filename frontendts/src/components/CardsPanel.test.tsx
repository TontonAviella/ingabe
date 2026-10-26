import { act } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CardsPanel } from '@/components/CardAnswer';
import { waitingOnPhoto } from '@/components/DroneCards';
import type { DroneCard } from '@/hooks/useDroneCards';
import { byButton, click, mount, unmountAll } from '@/test/dom';

// Roger, 2026-10-26: with the chats hidden, the "Chats" button sat on top of the hidden "Questions · 6" button, and
// that button gave a reader no reason to click it.
afterEach(() => {
  unmountAll();
  vi.useRealTimers();
});

function card(id: string, status: DroneCard['status'], extra: Partial<DroneCard> = {}): DroneCard {
  return { id, service: 1, service_name: 'Crops', question: `Question ${id}?`, preview: '', status, status_label: '', ...extra };
}

/** Renders and flushes at once, so the fake-timer test needs no polling. */
function show(ui: React.ReactElement) {
  act(() => {
    mount(ui);
  });
}

function hiddenPanel(props: Partial<React.ComponentProps<typeof CardsPanel>> = {}) {
  return (
    <CardsPanel
      ariaLabel="Questions"
      heading="Ask this photo"
      hideLabel="Hide the questions"
      collapsed
      collapsedLabel="4 answers ready on this photo"
      onCollapsedChange={vi.fn()}
      historyOpen={false}
      {...props}
    >
      <p>cards</p>
    </CardsPanel>
  );
}

describe('the hidden questions button', () => {
  it('moves below the Chats button on wide screens while the chats are hidden', () => {
    show(hiddenPanel());
    expect(byButton(/answers ready/)?.className).toContain('xl:top-4');
    unmountAll();
    show(hiddenPanel({ belowChatsButton: true }));
    const button = byButton(/answers ready/);
    expect(button?.className).toContain('top-16');
    expect(button?.className).not.toContain('xl:top-4');
  });

  it('shows a real question and opens that question when clicked', () => {
    const onTeaser = vi.fn();
    show(hiddenPanel({ teasers: [{ id: 'bare', text: 'Where is the soil bare?' }], onTeaser }));
    const button = byButton(/Where is the soil bare\?/);
    expect(button).not.toBeNull();
    click(button as HTMLButtonElement);
    expect(onTeaser).toHaveBeenCalledWith('bare');
  });

  it('turns to the next question after a few seconds, then holds still', () => {
    vi.useFakeTimers();
    show(
      hiddenPanel({
        teasers: [
          { id: 'a', text: 'First?' },
          { id: 'b', text: 'Second?' },
        ],
        onTeaser: vi.fn(),
      }),
    );
    expect(byButton(/First\?/)).not.toBeNull();
    act(() => vi.advanceTimersByTime(6000));
    expect(byButton(/Second\?/)).not.toBeNull();
    // 2 questions, 3 rounds: 5 turns in all, then it stays on the last one.
    const seen = [];
    for (let tick = 0; tick < 10; tick++) {
      act(() => vi.advanceTimersByTime(6000));
      seen.push(byButton(/answers ready/)?.textContent?.replace('4 answers ready on this photo', ''));
    }
    expect(seen).toEqual(['First?', 'Second?', 'First?', 'Second?', 'Second?', 'Second?', 'Second?', 'Second?', 'Second?', 'Second?']);
  });

  it('opens the panel when there is no question to show', () => {
    const onCollapsedChange = vi.fn();
    show(hiddenPanel({ onCollapsedChange }));
    click(byButton(/answers ready/) as HTMLButtonElement);
    expect(onCollapsedChange).toHaveBeenCalledWith(false);
  });
});

describe('what the hidden button says about a drone photo', () => {
  it('counts the answers ready and leads with them', () => {
    const waiting = waitingOnPhoto([card('a', 'needs_flight'), card('b', 'ready'), card('c', 'partly')]);
    expect(waiting.label).toBe('2 answers ready on this photo');
    expect(waiting.teasers.map((t) => t.id)).toEqual(['b', 'c']); // not 'a': it has no answer yet
  });

  it('puts new answers first', () => {
    const waiting = waitingOnPhoto([card('a', 'ready'), card('b', 'ready', { new: true })]);
    expect(waiting.label).toBe('1 new answer on this photo');
    expect(waiting.teasers[0].id).toBe('b');
  });

  it('says questions, not answers, when the photo answers none yet', () => {
    const waiting = waitingOnPhoto([card('a', 'needs_flight')]);
    expect(waiting.label).toBe('1 question about this photo');
    expect(waiting.teasers.map((t) => t.id)).toEqual(['a']);
    expect(waitingOnPhoto(null).label).toBe('Reading your photo');
  });
});
