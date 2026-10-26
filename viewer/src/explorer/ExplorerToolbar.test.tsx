import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ModelUsage } from '@/core/sessionScope';
import { ExplorerToolbar } from './ExplorerToolbar';

const usage = (model: string): ModelUsage => ({
  model,
  requests: 1,
  cost: 0,
  tokens: 0,
});
const models = ['claude-opus-4-5', 'claude-sonnet-4-5', 'claude-haiku-4-5'].map(
  usage
);

let root: Root | null = null;

beforeAll(() => {
  (
    globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
  ).IS_REACT_ACT_ENVIRONMENT = true;
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  Element.prototype.scrollIntoView ??= () => {};
});

afterEach(() => {
  act(() => root?.unmount());
  root = null;
});

function renderToolbar(
  model: string | null = null,
  onModel: (model: string | null) => void = () => {}
) {
  const host = document.body.appendChild(document.createElement('div'));
  root = createRoot(host);
  act(() =>
    root!.render(
      <ExplorerToolbar
        tasks={[]}
        task={null}
        onTask={() => {}}
        agents={[]}
        lens="main"
        lensLabel={null}
        onLens={() => {}}
        models={models}
        model={model}
        onModel={onModel}
        filtered={false}
        onClear={() => {}}
      />
    )
  );
}

function press(key: string) {
  act(() => {
    document.activeElement!.dispatchEvent(
      new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
    );
  });
}

const activeOption = () => {
  const id = document.activeElement?.getAttribute('aria-activedescendant');
  return id ? document.getElementById(id)?.textContent : null;
};

describe('ModelPicker', () => {
  it('opens on the current model with focus in the list, and ArrowDown then Enter picks the next one', async () => {
    const onModel = vi.fn();
    renderToolbar('claude-opus-4-5', onModel);
    const trigger = document.querySelector<HTMLButtonElement>(
      'button[aria-label="Model: claude-opus-4-5"]'
    )!;
    await act(async () => trigger.click());
    await vi.waitFor(() =>
      expect(document.activeElement?.getAttribute('role')).toBe('listbox')
    );
    expect(document.activeElement?.getAttribute('aria-label')).toBe('Models');

    press('ArrowDown');
    expect(activeOption()).toContain('claude-sonnet-4-5');
    press('Enter');

    expect(onModel).toHaveBeenCalledExactlyOnceWith('claude-sonnet-4-5');
  });
});

describe('CopyLinkButton', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('copies window.location.href to clipboard and shows brief Copied confirmation', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, {
      clipboard: { writeText },
    });

    renderToolbar();

    const copyBtn = document.querySelector<HTMLButtonElement>(
      'button[aria-label="Copy link to current view"]'
    )!;
    expect(copyBtn).toBeTruthy();
    expect(copyBtn.textContent).toContain('Copy link');

    await act(async () => {
      copyBtn.click();
    });

    expect(writeText).toHaveBeenCalledExactlyOnceWith(window.location.href);
    expect(copyBtn.textContent).toContain('Copied');
    expect(copyBtn.getAttribute('aria-label')).toBe('Link copied');

    // Advance 2s timer to ensure it resets back to "Copy link"
    act(() => {
      vi.advanceTimersByTime(2000);
    });

    expect(copyBtn.textContent).toContain('Copy link');
    expect(copyBtn.getAttribute('aria-label')).toBe('Copy link to current view');
  });
});