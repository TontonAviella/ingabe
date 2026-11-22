import { apiFetch } from '@mundi/ee';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FieldFlights } from '@/components/FieldFlights';
import { byButton, byText, click, mount, unmountAll } from '@/test/dom';

vi.mock('@mundi/ee', () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);

afterEach(() => {
  unmountAll();
  fetchMock.mockReset();
});

// Roger, 2026-11-22: "I thought you can read orthomosaic and know the date". The file's own day is offered where the
// day is asked; it becomes the flight day only when the reader takes it.
describe("the day a photo's own file gives", () => {
  it('is offered in the words of the server, and taken only when the reader says so', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ flown_on: '2026-03-16' }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    const said = 'The file says it was made on 16 March 2026. A photo map is often put together some days after the flight.';
    mount(<FieldFlights layerId="L1" flights={[]} flownOn={null} fileDay={{ day: '2026-03-16', said }} />);
    await vi.waitFor(() => expect(byText(said)).not.toBeNull());
    expect(fetchMock).not.toHaveBeenCalled(); // nothing is saved for the reader
    click(byButton('Use this day') as HTMLButtonElement);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/api/layer/L1/field-taps/flown-on');
    expect(init?.method).toBe('PUT');
    expect(JSON.parse(String(init?.body))).toEqual({ day: '2026-03-16' });
  });

  it('is only a sentence when the file gives no whole day', async () => {
    mount(
      <FieldFlights layerId="L1" flights={[]} flownOn={null} fileDay={{ day: null, said: 'This file does not say when it was flown.' }} />,
    );
    await vi.waitFor(() => expect(byText('This file does not say when it was flown.')).not.toBeNull());
    expect(byButton('Use this day')).toBeNull();
  });

  it('is not shown once the reader has given the day', async () => {
    mount(
      <FieldFlights
        layerId="L1"
        flights={[]}
        flownOn="2026-03-14"
        fileDay={{ day: '2026-03-16', said: 'The file says it was made on 16 March 2026.' }}
      />,
    );
    await vi.waitFor(() => expect(byText('Day this photo was flown')).not.toBeNull());
    expect(byButton('Use this day')).toBeNull();
    expect(byText('The file says it was made on 16 March 2026.')).toBeNull();
  });
});
