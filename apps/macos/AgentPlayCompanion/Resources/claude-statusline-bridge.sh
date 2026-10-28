#!/bin/sh
# AgentPlay Companion: Claude Code statusline bridge.
#
# Installed only when you turn on "Live Claude limits" in AgentPlay Companion, and removed when you turn it off.
# Claude Code runs this with its documented statusline JSON on stdin. It saves that JSON locally so the companion
# can show your 5-hour and weekly usage and each session's context, then runs the statusline you had before
# (if any) with the same input, so your status line looks the same. Nothing is sent anywhere.
dir="${AGENTPLAY_HOME:-$HOME/.agentplay}/live/claude"
mkdir -p "$dir" 2>/dev/null || exit 0
tmp="$dir/.in.$$"
cat > "$tmp" 2>/dev/null || exit 0
sid=$(grep -o '"session_id"[[:space:]]*:[[:space:]]*"[A-Za-z0-9_-]*"' "$tmp" | head -1 | sed 's/.*"\([A-Za-z0-9_-]*\)"$/\1/')
[ -n "$sid" ] || sid=unknown
mv -f "$tmp" "$dir/$sid.json"
find "$dir" -name '*.json' -mtime +2 -delete 2>/dev/null
prev="$dir/../claude-statusline.previous"
if [ -s "$prev" ]; then
  exec /bin/sh -c "$(cat "$prev")" < "$dir/$sid.json"
fi
exit 0
