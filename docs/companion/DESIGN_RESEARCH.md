# Companion menu bar: design research and spec

Researched 2026-10-23. Goal: make the AgentPlay Companion feel like a Mac menu, not a web card stack. Source URLs and images are listed at the end. "Seen" means I opened the image. "Text" means I only read the description.

## 1. What the references actually are

- **Bloom** (runbloom.app) is a full AppKit/SwiftUI window app for running agents. Its menu bar item shipped in v1.13.0 (12 Sep 2026). The site has no screenshot of it, so everything below comes from changelog text. Up to two figures per provider sit in the menu bar "as numbers or as small bars". The menu lists every window each provider reports (session, weekly, model-scoped weeklies, credits). "Each row has a meter coloured by pace rather than by level, so a week being spent too quickly turns amber or red before it runs out, and says when it will." Plan name sits beside the provider name. A Settings > Menu Bar pane picks providers, drag order, up to two metrics each, numbers or bars, used or left, with a live preview. v1.16.0 adds separate toggles for "waiting" and "finished" counts in the bar. The 23 Aug entry says the menu shows "which window is nearest expiry".
- **blume.codes** is **Blume**, a cross-platform (Mac, Linux, Windows) tool for reviewing agent "deliveries" with evidence. It is not a menu bar app, and the site shows no menu bar UI. One idea is worth taking: its hero "sidecar" lists one line per session as `Agent · task title` (for example "Claude Code · Separate catalog sync detection"). That is the right density for a session row.
- **CodexBar** (seen: docs/codexbar.png). It is a real `NSMenu`, with SwiftUI cards hosted in `NSHostingView` items (secondhand, from instagit). The status icon is a 16x16 template image drawn as a tiny meter that dims when data is stale, plus an incident overlay. It is the closest thing to what we want.
- **Blimp-Labs claude-usage-bar** (seen: demo.png). The status item has a spark glyph plus two micro bars labelled `5h` and `7d`. The popover holds two meters, a segmented time range, a line chart, and a footer row.
- **hamed-elfayome Claude-Usage-Tracker** (seen: popover.png). An opaque dark popover with rounded cards, coloured percentages, a custom refresh and gear in the header, and a pace tick on each bar. It shows how a busy layout reads, and it is our anti-example.
- **exelban/Stats** (seen: menus.png, popups.png). The bar widgets come in many shapes (mini text, line, bar, pie, battery). The popups are info-dense, with small caps section headers ("DETAILS", "TOP PROCESSES") over label/value rows. Good for row rhythm, too dense for us.
- **Ice** (jordanbaird/Ice). It manages menu bar items and has no popover worth copying. Take only one thing: every function also has a hotkey.
- **Apple Battery, Wi-Fi, Control Center** (observed on macOS, no URL). Native menus with a bold 13pt title row, a trailing toggle or value, an 11pt secondary line, hairline separators, and "... Settings..." as the last plain row.

## 2. Comparison

| App | Status item | Surface | Section order | Meter | Type | Material | Click / hover / right-click | Settings | Width |
|---|---|---|---|---|---|---|---|---|---|
| CodexBar | 16pt template mini meter, dims when stale, incident badge | NSMenu + hosted SwiftUI | provider switcher, header (name, "Updated just now", plan), Session, Weekly, model weekly, Extra usage, Cost >, actions with symbols, Settings / About / Quit | thin full-width bar, 4pt, "N% used" leading, "Resets in 3h 53m" trailing, pace line under | 13 semibold titles, 13 regular values, 11-12 secondary | system menu material | click opens menu, rows highlight like menu items, Cost has a submenu chevron | plain "Settings..." row near the bottom | ~308pt |
| Bloom | up to 2 figures per provider, numbers or bars, cup glyph when Keep Awake is on | menu (text only, no image) | providers, every window sorted nearest expiry first, credits, Keep Awake | bar tinted by pace, not level | n/a | n/a | n/a | dedicated Menu Bar pane with live preview | n/a |
| Blimp claude-usage-bar | glyph + two 5h/7d micro bars | SwiftUI window popover | title, 5h, 7d, range picker, chart, footer | thick bar, green/yellow by level | 15 bold title, 13 body | tinted translucent | click opens popover | Refresh / Sign Out / Quit as footer text, toggle inline | ~340pt |
| Claude-Usage-Tracker | 5 styles (battery, bar, %, ...) | popover, detachable | profile picker + status, then 5 cards | coloured bar with pace tick, coloured % | 17 bold card titles | opaque dark, cards on top | click popover, right-click context menu | gear in header | ~280pt |
| Stats | many widget types per module | popup per module | big gauge, history, details, top processes | rings, area charts | 13 values, 10 caps headers | solid light/dark panel | click per module | gear per module | ~250pt |
| Apple Battery | template battery + % | NSMenu | title + value, power source, apps using energy, Settings... | battery glyph only | 13 bold title, 13 body, 11 secondary | menu material | standard menu | last row "Battery Settings..." | ~300pt |

What the good ones share: a real menu or something identical to it, system material, no cards, one bar style everywhere, values right-aligned on the same line as the label, and Settings/Quit as plain rows at the bottom.

## 3. Spec for our menu

### 3.1 Surface and size
- Keep `MenuBarExtra` with `.menuBarExtraStyle(.window)`. The HIG says "Display a menu, not a popover... Unless the app functionality you want to expose is too complex for a menu". Meters and two-line session rows need custom layout, so we keep the window style and reproduce menu behaviour by hand (hover, keyboard, dismiss on action). Do not use an arrowed `NSPopover`.
- Width **320pt**, fixed. Height fits the content, capped at 560pt, and only the Sessions list scrolls.
- **No background fill.** Delete `.background(Theme.paper(scheme))` in Popover.swift. The MenuBarExtra window already draws the system menu material and vibrancy. If a build renders it flat, put the content in an `NSVisualEffectView` (`material = .menu`, `blendingMode = .behindWindow`, `state = .followsWindowActiveState`). Never a hex colour.
- Content insets: 14pt horizontal for text. Hover highlights inset 5pt from the edge with a 6pt corner radius (native menu geometry). 6pt top and bottom padding.

### 3.2 Colour and type
- Colours: `.primary` (labelColor), `.secondary` (secondaryLabelColor), `Color(nsColor: .tertiaryLabelColor)`, `Color(nsColor: .separatorColor)`. There is one accent, `Color.accentColor`, so the user's system accent wins. Warnings use `.orange` and errors use `.red` (system colours), only on the meter fill and the error glyph. Delete the lime/paleLime/paper/card palette from Theme.swift.
- Type (SF Pro, the macOS text styles from the HIG):
  - Provider name: `.body.weight(.semibold)` 13pt.
  - Row title / meter label: `.body` 13pt regular.
  - Values ("2% used", "Resets in 3h 53m", times): `.subheadline` 11pt, `.secondary`, `.monospacedDigit()`.
  - Secondary line (repo, last action, pace): `.subheadline` 11pt `.secondary`. Use tertiary for the least important part.
  - Section header: 11pt semibold `.secondary`, sentence case ("Sessions", not "CURRENT RUNS"). No letter-spaced caps.
  - Nothing above 13pt anywhere. No mono font except for file paths.

### 3.3 Section order (top to bottom)
1. **Header row**: the AgentPlay template glyph (16pt), "AgentPlay" at 13 semibold, and the trailing "Updated 1m ago" at 11 secondary. If data is sample or stale, the trailing text says so ("Sample data", "Stale, 12m"). Never a pill.
2. **Providers**, one block per detected provider (Claude Code, Codex, Cursor), ordered by most constrained first:
   - Provider row: provider symbol or 16pt logo, name at 13 semibold, plan trailing at 11 secondary ("Max", "Plus"), and a `chevron.right` trailing that rotates 90 degrees when expanded.
   - Expanded, one meter block per window, sorted nearest reset first (Bloom):
     ```
     Session                                  
     [==========-------------------------------]   Gauge, 4pt tall
     2% used                       Resets in 3h 53m
     On pace, lasts to reset                        (only if pace is notable)
     ```
   - Collapsed, the provider row shows the two key numbers inline at 11 secondary: "12% · 40% wk".
3. Separator.
4. **Sessions** header, trailing count ("3 running"). At most 5 rows, then a "Show All Sessions..." row.
5. Separator (only if there are errors).
6. **Recent errors** header. At most 3 rows, newest first.
7. Separator.
8. Action rows with leading symbols: `arrow.clockwise` Refresh (Cmd R), `macwindow` Open AgentPlay (Cmd O).
9. Separator.
10. `Settings...` (Cmd ,) and `Quit AgentPlay` (Cmd Q) as plain text rows, no icons, as in CodexBar and Apple's menus.

Empty or error states replace sections 2 to 6 with a single 13pt line plus an 11pt secondary line ("No agents found" / "Install Claude Code or Codex, then reopen."). Never a paragraph.

### 3.4 Row anatomy
- **Session row**, 2 lines, about 36pt tall: a leading 8pt status dot (`circle.fill`; accent = working, `.orange` = waiting for you, tertiary = idle), title `Claude Code · payments-api` at 13 regular, trailing elapsed `4m` at 11 secondary monospaced. Second line, 11 secondary, indented to the title: last action, truncated in the middle ("Edited src/charge.test.ts").
- **Error row**, 2 lines: leading `exclamationmark.triangle.fill` in `.red` at 11pt, title `pnpm test failed 3x` at 13, trailing time at 11. Second line: `Claude Code · payments-api · exit 1`.
- **Action row**, 1 line, 22pt tall: 16pt symbol in `.secondary`, label at 13, trailing shortcut in tertiary ("⌘R").
- Every interactive row is the full width and highlights on hover.

### 3.5 Meter
- `Gauge(value: used, in: 0...1) {}` with `.gaugeStyle(.linearCapacity)` and `.tint(paceTint)`, at 4pt height. No percentage text inside the bar.
- Fill = used. Tint = pace, not level (Bloom). Let expected = elapsed / window length. If used <= 1.25 x expected, tint is `.accentColor`. Above that it is `.orange`, and above 1.5 x expected or at 90%+ used it is `.red`. A 70% week that is on pace stays accent.
- Leading value text is "N% used" by default. Clicking toggles it to "N% left", and the choice persists per window.
- Pace line appears only when it is not neutral: "Ahead of pace, runs out in ~1d 4h" or "Lasts to reset". 11pt tertiary.

### 3.6 Hover, keyboard, collapse
- Hover: `RoundedRectangle(cornerRadius: 6).fill(Color.accentColor)` behind the row. Title, values and symbols switch to white (`.primary` on accent via `.foregroundStyle(.white)`). No hover animation, matching NSMenu.
- Keyboard: Up/Down moves the highlight across interactive rows. Return activates. Right/Left expands or collapses a provider. Esc closes. Cmd R, Cmd O, Cmd , and Cmd Q work while the window is open. Clicking an item closes the window, except the expand/collapse and used/left toggles.
- Collapse: provider blocks only. The first provider is expanded by default, the rest collapsed, and the state persists in Prefs. Animate the height with `.easeOut(duration: 0.15)`. Sessions and errors never collapse. They truncate with a "Show All..." row instead.

### 3.7 Status item icon
- 18x16pt **template** image (`isTemplate = true`) so the system tints it for light, dark, selected and tinted menu bars. Two horizontal bars, each 14pt wide and 3pt tall with a 1.5pt corner radius, 2pt apart and vertically centred. Top = 5 hour session, bottom = weekly. The track is drawn at 30% alpha and the fill at 100% alpha, with fill length = used, for the most constrained provider (or the pinned one in Settings).
- Template images are alpha-only, so pace colour cannot appear in the icon. States are shown by shape:
  - Stale over 10 min: the whole image at 50% alpha (CodexBar dims).
  - A session waiting for input: a 4pt dot at the top-right corner.
  - A new error since the last open: a `exclamationmark.triangle.fill` badge replaces the dot.
- Optional, off by default: a trailing percentage text (`12%`) at the menu bar system font, monospaced digits.
- No "5h/7d" micro-labels (Blimp). They are illegible at 16pt.
- If no provider has limits, fall back to the AgentPlay mark as a template.

### 3.8 Dark and light
Everything above is a semantic colour or the system material, so it adapts with no `colorScheme` branches. Remove `@Environment(\.colorScheme)` from views. Test in light, dark, Increase Contrast and Reduce Transparency. Reduce Transparency must give a solid system background automatically. If it does not, the material is being overridden somewhere.

## 4. Click behaviour

| Target | Action |
|---|---|
| Status item, left click | Open or close the menu window |
| Status item, right click | A small native `NSMenu`: Refresh, Open AgentPlay, Settings..., Quit. `MenuBarExtraAccess` exposes the `NSStatusItem` but has no right-click API, so take `statusItem.button`, call `sendAction(on: [.leftMouseUp, .rightMouseUp])`, and branch on `NSApp.currentEvent?.type` |
| Status item, Option click | Open the full viewer (existing behaviour, keep it) |
| Provider row | Toggle expand/collapse. No navigation |
| Meter block | Toggle used/left for that window. Hovering shows a `.help()` tooltip with the exact reset time ("Resets Fri 14:00") |
| Session row | Open that session in the full viewer (`model.openViewer(session:)`) and close the menu. Cmd click reveals the repo in Finder |
| Error row | Open the viewer at that error's sequence. Muting moves to the viewer or a right-click on the row (`.contextMenu`: Open, Mute This Pattern, Copy Command) |
| Show All Sessions... | Open the viewer's session list |

One action per row. Secondary actions go in `.contextMenu`, never in inline buttons.

## 5. Anti-patterns in the current build (v1-now-light.png)

| Current | Why it is wrong | Replace with |
|---|---|---|
| Opaque `Theme.paper` white/near-black fill | Kills the menu material and vibrancy, so it looks like a web page pasted on the desktop | System menu material, no fill |
| Now / Limits / Learn tabs (capsule picker) | Tabs for three small lists hide the meters behind a click. No native menu has tabs | One scrolling column in the order in 3.3 |
| Bordered rounded cards per run | Stacked cards (as in Claude-Usage-Tracker) waste ~16pt per row and read as iOS | Flat rows with a hover highlight and separators between sections only |
| Lime "Worth a look" panel with "See sequence" / "Mute this pattern" pill buttons | Custom pills and a tinted panel shout. Two buttons per item | An error row. Click opens it. Mute goes in the context menu |
| "Open full AgentPlay" pill + gear pill in the footer | Not how Mac menus end | Plain rows: Open AgentPlay (Cmd O), Settings... (Cmd ,), Quit (Cmd Q) |
| "Sample data" pill, "Local" dot badge | Badges compete with the content | Trailing 11pt secondary text in the header |
| CURRENT RUNS / WORTH A LOOK letter-spaced caps | Web dashboard idiom | Sentence case 11pt semibold secondary |
| Long explanation paragraphs in empty, error and onboarding states | Menus are glanceable. Explanation belongs in Settings or the viewer | One title line + one 11pt line, at most 60 characters |
| 380pt width | Wider than every reference. Feels like a window | 320pt |
| Hex colours with `colorScheme` branches | Breaks Increase Contrast and the user's accent | Semantic colours + `accentColor` |

## 6. Sources

- Bloom site: https://runbloom.app/ (screenshot seen: https://runbloom.app/screenshots/overview-1240.webp, the main window, not the menu)
- Bloom changelog (v1.13.0 12 Sep 2026, v1.16.0 14 Sep 2026, 23 Aug 2026 entry): https://runbloom.app/changelog
- Blume: https://blume.codes (no menu bar imagery; sidecar tiles at /images/sidecar/*.svg)
- CodexBar: https://codexbar.app/ and https://github.com/steipete/CodexBar. Menu screenshot seen: https://github.com/steipete/CodexBar/raw/main/docs/codexbar.png
- CodexBar implementation notes (secondhand): https://instagit.com/steipete/CodexBar/codexbar-menubar-icon-rendering-architecture.md
- Blimp-Labs claude-usage-bar: https://github.com/Blimp-Labs/claude-usage-bar. Seen: https://github.com/Blimp-Labs/claude-usage-bar/raw/main/macos/Resources/demo.png
- Claude-Usage-Tracker: https://github.com/hamed-elfayome/Claude-Usage-Tracker. Seen: https://github.com/hamed-elfayome/Claude-Usage-Tracker/raw/main/.github/popover.png
- Stats: https://github.com/exelban/stats. Seen: https://cdn.mac-stats.com/assets/images/menus.png and https://cdn.mac-stats.com/assets/images/popups.png
- Ice: https://github.com/jordanbaird/Ice
- MenuBarExtraAccess: https://github.com/orchetect/MenuBarExtraAccess
- HIG: https://developer.apple.com/design/human-interface-guidelines/the-menu-bar ("Display a menu, not a popover", template symbols, 24pt bar height), /popovers, /materials, /typography (macOS: Body 13, Callout 12, Subheadline 11, Footnote/Caption 10), /color
- Current build: docs/companion/screens/v1-now-light.png, apps/macos/AgentPlayCompanion/Sources/AgentPlayCompanion/{Popover,Theme,Components}.swift
