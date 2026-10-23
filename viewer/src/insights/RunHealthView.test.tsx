import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NuqsTestingAdapter, type OnUrlUpdateFunction } from 'nuqs/adapters/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { HealthReport } from '@/core/health';
import { SessionMapSourceProvider } from '@/data/SourceContext';
import type { SessionMapSource } from '@/data/source';
import { PointsOnTheTable } from './RunHealthView';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const fix = (id: string) => ({
  id,
  title: `Fix ${id}`,
  component: 'efficiency',
  pattern: 'pattern',
  effect: 'effect',
  sessions: ['s1'],
  points: 1,
  repo: 'r',
  actions: [{ label: 'Add rule' }],
});
const report = (ids: string[]) =>
  ({
    fixes: ids.map(fix),
    applied: [],
    repos: [{ name: 'r', root: '/r', granted: true }],
    titles: {},
  }) as unknown as HealthReport;

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = host = null;
});

function mount(
  source: Partial<SessionMapSource>,
  ids: string[],
  nuqs: { searchParams?: string; onUrlUpdate?: OnUrlUpdateFunction } = {}
) {
  host = document.body.appendChild(document.createElement('div'));
  root = createRoot(host);
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  act(() =>
    root!.render(
      <NuqsTestingAdapter {...nuqs}>
        <QueryClientProvider client={client}>
          <SessionMapSourceProvider source={source as SessionMapSource}>
            <PointsOnTheTable h={report(ids)} range={{}} />
          </SessionMapSourceProvider>
        </QueryClientProvider>
      </NuqsTestingAdapter>
    )
  );
  return host;
}
const button = (el: HTMLElement, text: string) =>
  [...el.querySelectorAll('button')].find((b) => b.textContent?.includes(text)) as HTMLButtonElement;
const flush = () => act(async () => {});

describe('PointsOnTheTable evidence links', () => {
  it('clears the explorer search so the evidence session is not filtered out', async () => {
    const onUrlUpdate = vi.fn();
    const el = mount({}, ['a'], { searchParams: '?sq=unrelated&win=7d&tab=insights', onUrlUpdate });
    await act(async () => button(el, '1 session').click());
    // nuqs flushes the URL update on a later tick than the click.
    await vi.waitFor(() => expect(onUrlUpdate).toHaveBeenCalled());
    const url = onUrlUpdate.mock.calls.at(-1)![0].searchParams;
    expect(url.get('session')).toBe('s1');
    expect(url.has('sq')).toBe(false);
    expect(url.get('win')).toBe('7d');
  });
});

describe('PointsOnTheTable writes', () => {
  it('shows a failed per-card Apply instead of a dead button', async () => {
    const write = vi.fn().mockRejectedValue(new Error('file changed since preview'));
    const el = mount({ write }, ['a']);
    await act(async () => button(el, 'Add rule').click());
    // The mutation's error state lands on a later tick than the click, so poll rather than flush once.
    await vi.waitFor(async () => {
      await flush();
      expect(el.querySelector('[role="alert"]')?.textContent).toBe('file changed since preview');
    });
  });

  it('applies all fixes in order, stays disabled, and reports where it stopped', async () => {
    const settle: Array<(ok: boolean) => void> = [];
    const write = vi.fn(() => new Promise((res, rej) => settle.push((ok) => (ok ? res({}) : rej(new Error('boom'))))));
    const el = mount({ write }, ['a', 'b', 'c']);
    await act(async () => button(el, 'Apply 3 fixes').click());
    expect(write).toHaveBeenCalledTimes(1);
    expect(button(el, 'Apply 3 fixes').disabled).toBe(true);
    await act(async () => settle[0](true));
    expect(write).toHaveBeenCalledTimes(2);
    expect(button(el, 'Apply 3 fixes').disabled).toBe(true);
    await act(async () => settle[1](false));
    await flush();
    expect(write).toHaveBeenCalledTimes(2);
    expect(el.querySelector('[role="alert"]')?.textContent).toBe('Applied 1 of 3. "Fix b" failed: boom');
    expect(button(el, 'Apply 3 fixes').disabled).toBe(false);
  });

  it('clears the preview after a successful apply and reports a failed preview', async () => {
    const fixPreview = vi
      .fn()
      .mockResolvedValueOnce({ file: 'AGENTS.md', hash: 'h', before: '', after: 'x' })
      .mockRejectedValueOnce(new Error('nope'));
    const write = vi.fn().mockResolvedValue({});
    const el = mount({ write, fixPreview }, ['a']);
    await act(async () => button(el, 'Preview change').click());
    expect(el.textContent).toContain('AGENTS.md');
    await act(async () => button(el, 'Add rule').click());
    expect(write).toHaveBeenCalledWith('/api/fix', expect.objectContaining({ hash: 'h' }));
    expect(el.textContent).not.toContain('AGENTS.md');
    await act(async () => button(el, 'Preview change').click());
    expect(el.querySelector('[role="alert"]')?.textContent).toBe('Could not load the preview: nope');
  });
});
