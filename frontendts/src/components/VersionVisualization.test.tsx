import { apiFetch } from '@mundi/ee';
import type React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MapDrawerProvider, type useMapDrawer } from '@/components/MapDrawer';
import { SageLive } from '@/components/SageReply';
import VersionVisualization from '@/components/VersionVisualization';
import type { Conversation, MapTreeResponse, SanitizedMessage } from '@/lib/types';
import { allByText, byButton, byText, click, findButton, findText, mount, unmountAll, unnamedControls } from '@/test/dom';

vi.mock('@mundi/ee', () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);

beforeEach(() => {
  // the "Answers written for" select reads the reader's role
  fetchMock.mockImplementation(
    async () =>
      new Response(JSON.stringify({ audience: 'agronomist', source: 'user', options: [{ key: 'agronomist', label: 'Agronomist' }] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
  );
});
afterEach(() => {
  unmountAll();
  fetchMock.mockReset();
});

const nothing = vi.fn();
const DRAWER = { on: true, active: 'sage', open: nothing, toggle: nothing, close: nothing, slots: {}, setSlot: nothing } as ReturnType<
  typeof useMapDrawer
>;
const inDrawer = (ui: React.ReactElement) => mount(<MapDrawerProvider drawer={DRAWER}>{ui}</MapDrawerProvider>);

const at = (minute: number) => `2026-11-23T10:${String(minute).padStart(2, '0')}:00Z`;
const QUESTION = 'What is the rain forecast for the next 10 days here?';
const ANSWER =
  '**About 42 mm is forecast for the next 10 days.**\n\n- Most of it falls on 26 and 27 November.\n\nSources: Open-Meteo · CHIRPS';
const asked: SanitizedMessage = { id: 1, role: 'user', content: QUESTION, has_tool_calls: false, created_at: at(1) };
const step: SanitizedMessage = {
  id: 2,
  role: 'assistant',
  content: '',
  has_tool_calls: true,
  tool_calls: [{ id: 'call_1', tagline: 'Read the weather forecast', icon: 'wrench', code: null }],
  created_at: at(2),
};
const stepDone: SanitizedMessage = {
  id: 3,
  role: 'tool',
  has_tool_calls: false,
  tool_response: { id: 'call_1', status: 'success' },
  created_at: at(3),
};
const answered: SanitizedMessage = { id: 4, role: 'assistant', content: ANSWER, has_tool_calls: false, created_at: at(4) };

const tree = (messages: SanitizedMessage[]): MapTreeResponse => ({
  project_id: 'P1',
  tree: [{ map_id: 'M1', messages, fork_reason: null, created_on: at(0), diff_from_previous: null }],
});
const CHAT: Conversation = {
  id: 5,
  project_id: 'P1',
  owner_uuid: 'u',
  title: 'Rain in the next 10 days',
  created_at: at(0),
  updated_at: at(4),
  message_count: 4,
};
const STARTERS = ['How healthy are the crops on this photo?', 'Has it rained enough around this farm this season?'];

function panel(over: Partial<React.ComponentProps<typeof VersionVisualization>> = {}) {
  return (
    <VersionVisualization
      mapTree={tree([asked, step, stepDone, answered])}
      conversationId={5}
      currentMapId="M1"
      conversations={[CHAT]}
      setConversationId={nothing}
      activeActions={[]}
      {...over}
    />
  );
}

// Roger, 2026-11-23: people could not work out how to use Sage, and too much asked for the eye at once.
describe("Sage's side of the drawer, with no chat open", () => {
  it('offers questions to start from, and one tap asks', async () => {
    const onAsk = vi.fn();
    inDrawer(panel({ conversationId: null, starters: STARTERS, onAsk }));
    await findText('What do you want to know?');
    click(await findButton(STARTERS[0]));
    expect(onAsk).toHaveBeenCalledWith(STARTERS[0]);
  });

  it('lists the recent chats under them, and has no button that does nothing or does what the drawer already does', async () => {
    const setConversationId = vi.fn();
    inDrawer(panel({ conversationId: null, starters: STARTERS, onAsk: nothing, setConversationId, onClose: undefined }));
    click(await findButton(/^Rain in the next 10 days/)); // the row also says how long ago
    expect(setConversationId).toHaveBeenCalledWith(5);
    expect(byButton('New chat')).toBeNull(); // the screen is the new chat
    expect(byButton('Hide chats')).toBeNull(); // the drawer's own close button does it
    expect(document.querySelector('#chat-search')).toBeNull(); // one chat is read at a glance
    expect(unnamedControls()).toEqual([]);
  });

  it('shows the search once the chats are more than a glance', async () => {
    const many = Array.from({ length: 7 }, (_, i) => ({ ...CHAT, id: i + 1, title: `Chat ${i + 1}` }));
    inDrawer(panel({ conversationId: null, conversations: many }));
    await findButton(/^Chat 7/);
    expect(document.querySelector('#chat-search')).not.toBeNull();
  });
  it('shows a question asked from here at once, while its chat is being made', async () => {
    const live = (
      <SageLive waiting actions={[{ action_id: 'c', action: 'Creating new conversation...' }]} errors={[]} onDismissError={nothing} />
    );
    inDrawer(
      panel({
        conversationId: null,
        mapTree: tree([]),
        starters: STARTERS,
        onAsk: nothing,
        pendingQuestion: STARTERS[0],
        busy: true,
        live,
      }),
    );
    await findText('Creating new conversation…');
    expect(allByText(STARTERS[0])).toHaveLength(1); // the reader's own line, not the button it was
    expect(byButton(STARTERS[0])).toBeNull();
    expect(byText('What do you want to know?')).toBeNull();
  });

  it('still says what went wrong when the chats cannot be loaded', async () => {
    const failed = [{ id: 'e1', message: 'No connection. Try again when it returns.', timestamp: new Date() }];
    inDrawer(panel({ conversationsEnabled: false, live: <SageLive actions={[]} errors={failed} onDismissError={nothing} /> }));
    await findText('No connection. Try again when it returns.');
    expect(byText('Your chats could not be loaded. Try again in a moment.')).not.toBeNull();
  });
});

describe("Sage's side of the drawer, with a chat open", () => {
  it('shows the answer once, in the conversation, with its sources as chips and the question of whether it was right', async () => {
    inDrawer(panel());
    await findText('About 42 mm is forecast for the next 10 days.');
    expect(allByText('About 42 mm is forecast for the next 10 days.')).toHaveLength(1);
    expect(byText(QUESTION)).not.toBeNull();
    expect(byText('Read the weather forecast')).not.toBeNull(); // the step, in plain words
    expect(byText('Open-Meteo')).not.toBeNull();
    expect(byText('CHIRPS')).not.toBeNull();
    expect(byText(/Sources: Open-Meteo/)).toBeNull(); // not also as a line of the answer
    expect(allByText('Was this answer right?')).toHaveLength(1);
    expect(byText('Created the map')).toBeNull(); // where every chat starts, not something that happened in this one
    expect(byButton('Hide chats')).toBeNull();
    expect(unnamedControls()).toEqual([]);
  });

  it('shows a question at once, before the conversation holds it, and then only once', async () => {
    const next = 'And the 10 days after that?';
    inDrawer(panel({ pendingQuestion: next, busy: true }));
    await findText(next);
    expect(byText('Was this answer right?')).toBeNull(); // Sage is at work on the next question
    unmountAll();
    // the conversation holds it now: the map screen stops passing it (lib/sagePending.ts)
    const later: SanitizedMessage = { id: 5, role: 'user', content: next, has_tool_calls: false, created_at: at(5) };
    inDrawer(panel({ pendingQuestion: null, busy: true, mapTree: tree([asked, step, stepDone, answered, later]) }));
    await findText(next);
    expect(allByText(next)).toHaveLength(1);
  });

  it('does not ask whether words said on the way to a step were right', async () => {
    const onTheWay: SanitizedMessage = { ...step, id: 6, content: 'Let me read the forecast.' };
    inDrawer(panel({ mapTree: tree([asked, onTheWay]) }));
    await findText('Let me read the forecast.');
    expect(byText('Was this answer right?')).toBeNull();
  });

  it('says what Sage is doing on one line at the end, and what went wrong', async () => {
    const actions = [
      { action_id: 'a', action: 'Sending message to Sage...' },
      { action_id: 'b', action: 'Reading the weather forecast' },
    ];
    const live = <SageLive waiting actions={actions} errors={[]} onDismissError={nothing} />;
    inDrawer(panel({ mapTree: tree([asked]), busy: true, live }));
    await findText('Reading the weather forecast');
    expect(byText('Sending message to Sage…')).toBeNull(); // the step in hand, not a list
    expect(byText('Sage is working…')).toBeNull(); // said once
    unmountAll();

    const onDismissError = vi.fn();
    const failed = [{ id: 'e1', message: 'Sage could not be reached. Try again.', timestamp: new Date() }];
    inDrawer(panel({ mapTree: tree([asked]), live: <SageLive waiting actions={[]} errors={failed} onDismissError={onDismissError} /> }));
    await findText('Sage could not be reached. Try again.');
    expect(byText('Thinking…')).toBeNull(); // an error ends the wait
    click(byButton('Dismiss') as HTMLButtonElement);
    expect(onDismissError).toHaveBeenCalledWith('e1');
  });

  it('shows nothing live once the answer is in the conversation', async () => {
    const host = mount(<SageLive waiting={false} actions={[]} errors={[]} onDismissError={nothing} />);
    expect(host.textContent).toBe('');
  });
});

describe('the chats panel where the drawer is off (the old placing)', () => {
  it('is as it was: its own hide button, the working line, and no rating (the answer card asks)', async () => {
    const onClose = vi.fn();
    mount(panel({ onClose, activeActions: [{ action_id: 'a', action: 'x' } as never], starters: STARTERS, onAsk: nothing }));
    await findText('About 42 mm is forecast for the next 10 days.');
    expect(byText('Sage is working…')).not.toBeNull();
    expect(byText('Was this answer right?')).toBeNull();
    click(byButton('Hide chats') as HTMLButtonElement);
    expect(onClose).toHaveBeenCalled();
  });
});
