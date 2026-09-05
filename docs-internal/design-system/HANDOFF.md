# Handoff: OpenCompany Theme System (12 themes × 17 panels)

> **Merged copy (2026-08)** — this is the handoff brief from the external
> `design_handoff_theme_system/` bundle, merged into
> `docs-internal/design-system/` as the canonical location. The external
> bundle was verified fully migrated (every directory diffed against its
> in-repo counterpart) and **removed from disk on 2026-08-14**; a
> `.gitignore` entry guards against a re-drop. Path mapping of what it
> contained: its `themes/` was byte-identical to the live
> [`client/src/themes/`](../../client/src/themes/) (which is authoritative
> and has since moved ahead via the fidelity pass); its `tokens/` /
> `components/` / `guidelines/` / `ui_kits/` are this directory's
> same-named folders; its `_source_docs/` are this directory's root files;
> `reference-mockup/` lives here at
> [`reference-mockup/`](./reference-mockup/); its `assets/diagrams/` were
> stripped exports of the richer sources already in
> [`assets/diagrams/`](./assets/diagrams/).
>
> **Product amendments** (decisions recorded after the fidelity pass —
> where this brief and the product deliberately differ, the amendment
> wins):
>
> 1. **Themes CSS is authoritative over the mockup.** The shared 1.4s
>    `--pulse-duration` token drives both node families (the mockup's
>    synthesized 1.5s square-node duration and its `--accent-text-mix`
>    variable are mockup-only inventions, not product tokens).
> 2. **Fonts: legibility substitutions ADOPTED.** Cyber ships Space Mono
>    and Plague ships Cinzel as their display faces (see §Legibility note
>    below — the recommendation was taken).
> 3. **Toolbar: the one shrinkable group is the workflow-name chip.**
>    The live toolbar has no wordmark; long workflow names are the real
>    overflow source, so the name chip carries `min-w-0 flex-1 truncate`
>    and every other cluster is pinned `shrink-0`.
> 4. **Edges:** resting edges follow this brief (pale neutral
>    `--edge-stroke`, dashed, orthogonal step); during execution the
>    status classes recolor with semantic tokens (executing / completed /
>    error / pending / memory- / tool- / skill-active) — runtime feedback
>    is functionality this product keeps. A condition-carrying
>    `ConditionalEdge` highlights in `--accent` — a sanctioned deviation.
> 5. **Console Dock is a hybrid**, not plain three tabs: Chat / Console /
>    Terminal tabs plus a split-view toggle that docks Chat beside the
>    Console/Terminal pane (simultaneous chat + logs during agent runs is
>    load-bearing functionality).
> 6. **Themed glyphs: ten themes ship their own set** (renaissance, cyber,
>    greek, edo, steampunk, atomic, wasteland, rot, plague, surveillance);
>    only Light and Dark fall through to lucide. The "eight themes" claim
>    later in this brief is stale.

## Overview

OpenCompany is a desktop workflow canvas for building agent workflows. It ships **12 complete visual themes** — two neutral base themes plus ten "world" skins (Renaissance, Greek, Edo, Steampunk, Atomic, Cyber, Wasteland, Rot, Plague, Surveillance). Every theme reskins the *entire* application: surfaces, type (a different display/body/mono trio per theme), radii, iconography, canvas background texture, and motion.

This bundle contains the design system's real source files plus a working reference mockup that renders **every panel in every theme on one canvas**, so you can diff your implementation against the intended result panel-by-panel.

The theming architecture is the whole point: a component is written **once**, against tokens, and picks up all 12 skins for free. If you find yourself writing `if (theme === 'cyber')` in a component, the token layer is missing something — add the token instead.

## About the Design Files

The files in `reference-mockup/` are a **design reference created in HTML** — a prototype showing intended look and behavior, not production code to copy. Recreate the designs in the target codebase's existing environment (this app is React + TypeScript + Tailwind + React Flow) using its established patterns.

The files in `themes/`, `tokens/`, `components/`, and `guidelines/` are **the real design system**, lifted verbatim from the repo. Those are not references — they are the source of truth, and the CSS in `themes/` and `tokens/` can be used directly.

## Fidelity

**High-fidelity.** Exact colors, type, spacing, radii, motion curves and glow values throughout, all traceable to the CSS in this bundle. Recreate pixel-perfectly.

---

## What's in the bundle

```
design_handoff_theme_system/
├─ themes/            ← 14 CSS files. THE SOURCE OF TRUTH. Ship these.
│                       base.css (contract) + animations.css + 12 theme files
├─ tokens/            ← 7 CSS files: the distilled token layer, well commented.
│                       Read these first — they explain the system.
├─ components/        ← 106 files. The component library, grouped by family
│                       (buttons/ canvas/ display/ feedback/ forms/ icons/ panels/).
│                       Each component ships .jsx + .d.ts + .prompt.md.
│                       The .prompt.md files are the per-component contracts —
│                       read them before implementing that component.
├─ guidelines/        ← THEMES.md + ANIMATIONS.md (prose contracts) and 21
│                       standalone HTML specimen pages (type scale, radii,
│                       node roles, shadows, all-theme comparisons).
├─ ui_kits/opencompany/ ← A runnable recreation of the app shell
│                       (App / CanvasView / Toolbar / Panels / ConsoleDock).
├─ reference/themes/  ← The design system's own distilled copy of the 12 themes.
│                       Useful for reading; `themes/` is authoritative.
├─ assets/            ← Product canvas screenshot + 6 architecture diagrams.
├─ reference-mockup/  ← The panel × theme matrix (open the .dc.html in a browser).
└─ _source_docs/      ← DS readme, IMPLEMENTATION.md, SKILL.md, manifest,
                        and _adherence.oxlintrc.json (lint rules that enforce
                        token usage — worth wiring into CI).
```

**Read in this order:** `tokens/*.css` → `guidelines/THEMES.md` → `themes/base.css` → one theme file (`themes/cyber.css` is the most extreme) → `components/*/**.prompt.md` for whatever you're building.

---

## Architecture: a three-layer cascade

1. **`tokens/`** — names the design decisions (`--bg-panel`, `--node-agent`, `--dur-fast`). Light values live in `:root`; dark overrides live under `.dark, [data-theme="dark"]`.
2. **`themes/base.css`** — the structural contract every theme must satisfy: which tokens exist, the node glow ladder, the canvas layer stack, motion defaults.
3. **`themes/<name>.css`** — one theme = one `[data-theme="x"]` block that re-declares the token set. Themes may add signature keyframes and ornament layers, but must not restyle components directly.

Theme switching is a single attribute on `<html>`: `data-theme="cyber"`. Nothing else changes.

---

## Design tokens

### Surfaces (light `:root` → dark)
| Token | Light | Dark |
|---|---|---|
| `--bg-app` | `#f5f7fa` | `#0d0f13` |
| `--bg-panel` | `#fafbfc` | `#15171c` |
| `--bg-canvas` | `#ffffff` | `#0d0f13` |
| `--bg-elevated` | `#ffffff` | `#1b1e25` |
| `--bg-input` | `#ffffff` | `#15171c` |
| `--surface-card` | `#ffffff` | `#1b1e25` |
| `--bg-hover` | `rgba(0,0,0,.04)` | `rgba(255,255,255,.05)` |
| `--bg-active` | `rgba(0,0,0,.06)` | `rgba(255,255,255,.08)` |
| `--bg-overlay` | `rgba(0,0,0,.45)` | `rgba(0,0,0,.7)` |

### Foreground & borders
| Token | Light | Dark |
|---|---|---|
| `--fg-default` | `#1a1d21` | `#e8eaed` |
| `--fg-muted` | `#4b5563` | `#9aa1ac` |
| `--fg-faint` | `#9ca3af` | `#6b7280` |
| `--border-default` | `#d1d5db` | `#2b2f37` |
| `--border-strong` | `#9ca3af` | `#3c424c` |
| `--border-focus` | `#3b82f6` | `#3b82f6` |

### Semantic status
`--success` `#059669`→`#22c55e` · `--warning` `#d97706`→`#f59e0b` · `--destructive` `#dc2626`→`#ef4444` · `--info` `#0891b2`→`#38bdf8`

### Action intents (toolbar soft-tinted buttons)
Six intents — `run` (deploy), `stop` (destructive), `save` (commit), `config` (settings), `secret` (credentials), `tools` (palette). Each has four tokens plus an ink alias:

```
--action-run          the raw hue (Dracula green #50fa7b)
--action-run-soft     color-mix(… 15%, transparent)   ← the fill
--action-run-hover    color-mix(… 25%, transparent)
--action-run-border   color-mix(… 60%, transparent)   ← the border
--action-run-ink      #15803d light / #50fa7b dark    ← the TEXT color
```

**Use `-ink` for text, never the raw hue.** The vivid Dracula palette is bright enough to be text on dark grounds but fails contrast on light ones; the ink aliases resolve per mode. This is the single easiest thing to get wrong.

### Node role colors (canvas)
`--node-agent` purple · `--node-model` cyan · `--node-tool` green · `--node-trigger` pink · `--node-workflow` orange. Each also has `-soft` (8%) and `-border` (30%) variants. Plus `--node-pulse-color` — see the glow ladder below.

### Typography
Base **14px** (dense desktop tool). Scale: `--text-2xs` 11 · `--text-xs` 12 · `--text-sm` 13 · `--text-base` 14 · `--text-md` 16 · `--text-lg` 18 · `--text-xl` 24 · `--text-2xl` 32 · `--text-3xl` 44.
Weights 400/500/600/700. Leading 1.2 / 1.5 / 1.65.
Base fonts: **Geist** (sans/display/body), **JetBrains Mono** (mono).

Three tokens carry the themed typography and are the reason components need no theme branching:
```
--font-display  --font-body  --font-mono
--type-uppercase          none | uppercase
--type-tracking-display   0 | 0.04em … 0.18em
```
Every theme re-declares all five. A heading is written once as `font-family: var(--font-display); text-transform: var(--type-uppercase); letter-spacing: var(--type-tracking-display)`.

### Spacing, radii, chrome
Space scale 4 / 8 / 12 / 16 / 24 / 32 / 48 / 64 (`--space-1`…`--space-8`).
Radii `--radius-sm` 4 · `--radius-md` 6 · `--radius-lg` 8 · `--radius-xl` 12 · `--radius-node` 10 · `--radius-pill` 999. Themes override these wholesale — Greek/Edo/Cyber/Wasteland/Plague/Surveillance go to **0** across the board.
Fixed chrome: `--h-toolbar` 48 · `--h-statusbar` 24 · `--h-control` 32 · `--w-sidebar` 280 · `--w-palette` 320.

### Shadows & motion
`--shadow-card`, `--shadow-card-hover`, `--shadow-modal` (all darker in dark mode).
`--dur-fast` 90ms · `--dur-default` 180ms · `--dur-slow` 320ms · `--ease-default` `cubic-bezier(.2,.7,.3,1)` · `--ease-emphasis` `cubic-bezier(.6,-.05,.3,1.4)`.
**Themes override durations and easing, not just colors** — Cyber runs 60/120/240 with `steps()` glitch easing, Rot drifts at 140/320/680. Personality lives in the curve. See `guidelines/ANIMATIONS.md`.

---

## The 12 themes

| Theme | Family | Character | Display / Body / Mono |
|---|---|---|---|
| Light | base | grey-blue paper workspace | Geist / Geist / JetBrains Mono |
| Dark | base | neutral slate + Dracula neon | Geist / Geist / JetBrains Mono |
| Renaissance | utopian | illuminated codex, gilded | Cinzel / Cormorant Garamond / IM Fell English |
| Greek | utopian | sun-bleached marble agora | Cinzel / Cormorant Garamond / Courier Prime |
| Edo | utopian | washi paper + sumi-e ink | Shippori Mincho ×2 / JetBrains Mono |
| Steampunk | utopian | brass, copper, riveted leather | IM Fell English SC / IM Fell English / Special Elite |
| Atomic | utopian | 1962 Eames optimism | Bevan / Lato / Space Mono |
| Cyber | dystopian | Neuromancer night market | *see note* / JetBrains Mono ×2 |
| Wasteland | dystopian | irradiated scrap metal | Special Elite ×2 / VT323 |
| Rot | dystopian | moss-overgrown crypt | Pirata One / EB Garamond / JetBrains Mono |
| Plague | dystopian | 1349 quarantine broadsheet | *see note* / EB Garamond / Special Elite |
| Surveillance | dystopian | 1970s panopticon control room | Anonymous Pro / IBM Plex Mono ×2 |

**Legibility note (a recommendation, not a spec change):** Cyber ships **Major Mono Display** and Plague ships **UnifrakturCook** as their display faces. Both are effectively unreadable at UI sizes — Major Mono renders lowercase as sparse scattered fragments at 13px, and blackletter under `uppercase` + `.06em` tracking is close to unparseable. The reference mockup substitutes **Space Mono** and **Cinzel** respectively, which hold the same voice and stay legible. Decide whether to carry that substitution into the product; if you keep the originals, restrict them to large headings only.

---

## The node glow ladder

Defined in `themes/base.css`. Getting this right is most of the canvas's quality. Five states:

| State | Treatment |
|---|---|
| **Resting** | `box-shadow: 0 2px 8px color-mix(in srgb, <node-color> 18%, transparent)` — a quiet drop shadow. **No halo.** |
| **Hover** | `translateY(-1px)` + `0 4px 14px color-mix(… 28%, transparent)`, transitioned on `--dur-fast` / `--ease-default` |
| **Selected** | 1px accent ring + `0 4px 14px color-mix(… 32%, transparent)`, border goes to full strength |
| **Executing** | the theme's own `--pulse-keyframe`, driven by `--node-pulse-color`. Timing: `.node` **1.4s**, `.sq-node-box` **1.5s** |
| **Success / error** | `0 0 0 1px <semantic>, 0 0 14px color-mix(in srgb, <semantic> 60%, transparent)` |

Three traps, all of which I hit:

1. **The halo belongs to the pulse and to outcome states — not to the resting node.** A permanent accent halo on every node reads as "everything is running."
2. **A `box-shadow` keyframe overwrites any static `box-shadow` every frame.** So a `.node.selected` ring is invisible on an executing node. Don't combine the two states; pick one.
3. **An author `!important` hover shadow outranks a running animation and freezes the pulse.** Animated nodes must get **transform-only** hover; only static nodes get the hover shadow.

Also: `--node-pulse-color` — not the node's own accent — drives the pulse, so it stays legible on Renaissance vellum and Cyber's near-black void alike. Status pips follow `data-status`: executing/waiting take the node color + a 6px halo and blink; success/error take their semantic token.

---

## The canvas background

Three stacked layers, bottom to top:

1. **`--bg-canvas`** — the base fill.
2. **The `.canvas` gradient stack** — per-theme atmosphere (Renaissance's darkening vignette, Edo's sumi-e wash at 80%/90%, Atomic's five confetti dots, Steampunk's copper rivet-heads, Rot's two amber decay pools).
3. **`--canvas-grid`** — the pattern. **Eight of the ten skins define this as an SVG data-url, not a gradient:** Greek's key meander (80px tile), Steampunk's rivets, Wasteland's cracks (200px), Renaissance's fleur-de-lis, Plague's quarantine X's (240px), Rot's lattice, Atomic's starburst (160px), Surveillance's CCTV corner brackets + centre reticle.

Two asymmetries worth knowing, because they look like bugs: **Edo has no `--canvas-grid` at all** (only the wash), and **Greek, Plague, Wasteland and Surveillance have no gradient stack** (pattern straight over `--bg-canvas`). Don't invent the missing layer for symmetry.

Cyber additionally overlays CRT scanlines and a rolling scan band; Surveillance's reticle arrives *inside* its grid SVG — don't draw a second one.

---

## Panels to implement (17)

Each is in the reference mockup, rendered in all 12 themes.

| # | Panel | Size | Source |
|---|---|---|---|
| 1 | Workflow Sidebar | 280px | `ui/WorkflowSidebar.tsx` |
| 2 | Component Palette | 320px | `ui/ComponentPalette.tsx` |
| 3 | Node Configuration | 3-column modal | `ParameterPanel.tsx` |
| 4 | Settings | modal | `ui/SettingsPanel.tsx` |
| 5 | API Credentials | modal | `credentials/CredentialsModal.tsx` |
| 6 | Console Dock | tabbed | `ui/ConsolePanel.tsx` |
| 7 | Output Panel | — | `output/OutputPanel.tsx` |
| 8 | Input Data Panel | 350px | `ui/InputNodesPanel.tsx` |
| 9 | Context Panel | — | `parameterPanel/ContextPanel.tsx` |
| 10 | Memory Tool Panel | — | `parameterPanel/MemoryToolPanel.tsx` |
| 11 | Task Manager Panel | — | `parameterPanel/TaskManagerPanel.tsx` |
| 12 | Team Monitor Panel | — | `parameterPanel/TeamMonitorPanel.tsx` |
| 13 | Process Manager Panel | 9-col table | `parameterPanel/ProcessManagerPanel.tsx` |
| 14 | Gallery Panel | grid ⇄ list | `parameterPanel/GalleryPanel.tsx` |
| 15 | Toolbar + Status Bar | 48 + 24px | `ui/TopToolbar.tsx`, `StatusBar` |
| 16 | Node Canvas | — | `Dashboard.tsx`, `SquareNode.tsx` |
| 17 | Motion + Animations | — | `tokens/animations.css` |

**Canvas edges are React Flow `step` edges** — dashed orthogonal runs with right-angle corners in one pale neutral stroke. Not beziers.

**Status bar** field set: `● ONLINE | WF: <name> | NODES: n | THEME: <name> | <clock>`.

---

## Layout traps under themed typography

The skins that use `uppercase` + wide tracking (Cyber at `.18em`, Greek at `.18em`, Renaissance/Steampunk/Wasteland/Surveillance at `.10em`) make **every label substantially wider** than in Light/Dark. Two consequences:

- **Pin your toolbar clusters.** The wordmark, workflow-name chip, mode toggle and action cluster all need `flex: none`; leave exactly one group (File/Edit/View) shrinkable. Without this the mode toggle collapses to a sliver in Cyber and Atomic.
- **Test the widest theme, not the default.** Anything that fits in Light will overflow somewhere. Cyber and Greek are the stress cases.

Also: canvas node icons are **32px** everywhere, and the size is a token, not a class — `theme.nodeSize.squareIcon` in `client/src/styles/theme.ts` (0.5 x the 64px `nodeSize.square` box), passed to `NodeIcon` as `size=`; non-canvas surfaces use the `theme.iconSize.*` scale the same way. `NodeIcon` applies that one token as the box's width, height AND font size, so SVG, library and emoji icons all draw at the same edge. Never size an icon with Tailwind `h-*`/`text-*` classes: the theme type scale maps `text-3xl` to 44px, which is how emoji nodes once painted half again larger than every SVG-backed node beside them. Eight themes ship their own glyph set; Light, Dark, Plague and Surveillance fall through to lucide.

---

## Interactions & state

- **Theme switch** — `data-theme` on `<html>`; persist the choice.
- **Node select** — click sets selected state (ring + lift). Only one at a time.
- **Node execute** — pulse runs for the duration of the step, then resolves to success or error glow.
- **Panel tabs** (Console: Chat / Console / Terminal) — plain tab state.
- **Palette search** — filters sections live; empty state when no match.
- **Settings** — switches and sliders write through immediately (auto-save interval 10–300s, compaction ratio 5–95%).
- **Gallery** — grid ⇄ list toggle.
- **Reduced motion** — honor `prefers-reduced-motion`; the pulse and all ornament animations should stop.

---

## Suggested implementation order

1. Wire the token layer + `themes/base.css`, get Light and Dark correct. **Do not move on until a component has zero hardcoded colors.**
2. Add one extreme skin (Cyber) — it will expose every missing token, especially in type and radii.
3. Build the panels against tokens, checking each against the reference mockup.
4. Add the canvas: node glow ladder first, then step edges, then the background stack.
5. Layer in the remaining nine themes — mostly declarative once steps 1–3 are honest.
6. Wire `_source_docs/_adherence.oxlintrc.json` into CI to keep hardcoded values from creeping back.

## Assets

`assets/product-canvas-screenshot.png` — the real running canvas; the fidelity target for node/edge rendering.
`assets/diagrams/*.svg` — six architecture diagrams (node anatomy, execution flow, AI agent routing, system overview, default workflows, how-it-works).

## Files

- `reference-mockup/Panel Theme Matrix.dc.html` — open in a browser. Left rail picks the panel; header filters by theme family (All 12 / Base 2 / Utopian 5 / Dystopian 5), zooms, and toggles motion. Interactions are shared across themes on purpose: type in one palette search or toggle one switch and all 12 respond, so you can compare the same state across skins.
- `reference-mockup/support.js` — runtime for the above; keep it beside the HTML.
