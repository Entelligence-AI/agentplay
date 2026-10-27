<!-- One change per PR. Read "Using AI tools" in CONTRIBUTING.md: agents are welcome, but a
human must review the diff and own this PR. PRs with no human behind them will be closed. -->

## Summary

<!-- What changed and why, in a few lines a reviewer can trust. It must match the diff. Link the issue: Closes #N -->

## Test plan

<!-- What you ran and what you saw. For UI changes: before and after screenshots, light and dark
(`pnpm screenshots` in `viewer/`). Say what you did not test. -->

- [ ] `make check` passes
- [ ] No prompt text, model output, tool input/output, file contents or command arguments can reach a map, a log line or the viewer ([redaction contract](https://github.com/Entelligence-AI/agentplay/blob/main/docs/format.md#redaction-contract))
- [ ] No new network calls or Python runtime dependencies
