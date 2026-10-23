#!/bin/sh
# Remove AgentPlay Companion and everything it created. Your session maps in ~/.agentplay stay,
# unless you pass --remove-maps (that also removes what `uvx agentplay` built: it's the same store).
set -eu
REMOVE_MAPS=0
[ "${1:-}" = "--remove-maps" ] && REMOVE_MAPS=1
APP="/Applications/AgentPlay Companion.app"
DOMAIN=ai.entelligence.agentplay.companion

pkill -f "AgentPlay Companion.app/Contents/MacOS" 2>/dev/null || true
sleep 1
# Its viewer, if one outlived it (only this app's own Python, never a viewer you started).
pkill -f "AgentPlay Companion.app/Contents/Resources/python/bin/python3 -I -m agentplay view" 2>/dev/null || true
# The login item: macOS drops it with the app; this clears a stale entry.
: # SMAppService login items go away with the app bundle; nothing to reset globally.
defaults delete "$DOMAIN" 2>/dev/null || true
rm -rf "$HOME/Library/Preferences/$DOMAIN.plist" "$HOME/Library/Caches/$DOMAIN" \
       "$HOME/Library/Saved Application State/$DOMAIN.savedState" "$HOME/Library/HTTPStorages/$DOMAIN"
if [ -d "$APP" ]; then rm -rf "$APP"; echo "Removed $APP and its login item."; else echo "No app in /Applications."; fi
echo "Removed its preferences and caches."
echo "Delivered notifications disappear with the app. Notification permission: System Settings → Notifications."
if [ "$REMOVE_MAPS" = 1 ]; then
  rm -rf "${AGENTPLAY_HOME:-$HOME/.agentplay}"
  echo "Removed ${AGENTPLAY_HOME:-$HOME/.agentplay} (all AgentPlay maps)."
else
  echo "Kept ${AGENTPLAY_HOME:-$HOME/.agentplay} (your session maps). Pass --remove-maps to delete them too."
fi
