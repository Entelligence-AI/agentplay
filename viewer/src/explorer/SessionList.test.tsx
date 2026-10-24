import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it } from 'vitest';
import { sessionExplorerSessionSchema } from '@/core/schema';
import { SessionList } from './SessionList';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const session = (seqs: number[]) =>
  sessionExplorerSessionSchema.parse({
    sessionId: 's1',
    startedAt: '2026-01-02T03:04:05Z',
    search: { count: seqs.length, seqs, chips: [] },
  });

let host: HTMLElement | null = null;
afterEach(() => {
  host?.remove();
  host = null;
});

function render(seqs: number[]) {
  host ??= document.body.appendChild(document.createElement('div'));
  const root = (host as HTMLElement & { _root?: ReturnType<typeof createRoot> });
  root._root ??= createRoot(host);
  act(() =>
    root._root!.render(
      <SessionList
        sessions={[session(seqs)]}
        selectedId="s1"
        onSelect={() => {}}
        onOpen={() => {}}
        search="x"
        onSearch={() => {}}
        hasMore={false}
        loadingMore={false}
        onLoadMore={() => {}}
        onJump={() => {}}
      />
    )
  );
  return host;
}

describe('SearchMatches stepper', () => {
  it('restarts at the first match when the match list changes', () => {
    const el = render([1, 2, 3, 4, 5]);
    const next = () => el.querySelector<HTMLButtonElement>('[aria-label="Next match"]')!;
    for (let k = 0; k < 4; k++) act(() => next().click());
    expect(el.textContent).toContain('Match 5 of 5');
    render([7, 8]);
    expect(el.textContent).toContain('Match 1 of 2');
  });
});
