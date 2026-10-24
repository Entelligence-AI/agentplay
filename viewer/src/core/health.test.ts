import { describe, expect, it } from 'vitest';
import { actionText, addedLines, healthReportSchema } from './health';

describe('health helpers', () => {
  it('marks only the lines a fix adds', () => {
    const lines = addedLines('# Notes\n\nKeep.\n', '# Notes\n\nKeep.\n\n- Run `pytest -q`.\n');
    expect(lines.filter((l) => l.added).map((l) => l.text)).toEqual(['- Run `pytest -q`.']);
  });

  it('turns each action into text a person can paste', () => {
    expect(actionText({ kind: 'claude_md', label: 'x', file: 'CLAUDE.md', lines: ['A', 'B'] })).toBe('- A\n- B');
    expect(actionText({ kind: 'settings_allow', label: 'x', file: 's', entry: 'Bash(git push:*)' })).toContain('Bash(git push:*)');
    expect(actionText({ kind: 'stop_hook', label: 'x', file: 's', command: 'pytest' })).toContain('"command": "pytest"');
  });

  it('treats a report without fixes as having none', () => {
    expect(healthReportSchema.parse({ sessions: 3 }).fixes).toEqual([]);
  });

  it('keeps the readable fixes from a partly unreadable report', () => {
    const r = healthReportSchema.parse({ sessions: 3, fixes: [{ id: 'a', title: 'T' }, { nope: true }] });
    expect(r.fixes.map((f) => f.id)).toEqual(['a']);
    expect(r.score).toBeNull();
  });
});
