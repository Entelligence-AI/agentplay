import { describe, expect, it } from 'vitest';
import { sameOriginUrl } from './demoSource';

describe('sameOriginUrl', () => {
  const base = new URL('https://example.test/app/demo/');

  it('resolves demo files against the page', () => {
    expect(sameOriginUrl('index.json', base).href).toBe('https://example.test/app/demo/index.json');
  });

  it('refuses a file that points at another origin', () => {
    expect(() => sameOriginUrl('https://evil.test/map.json', base)).toThrow();
    expect(() => sameOriginUrl('//evil.test/map.json', base)).toThrow();
  });
});
