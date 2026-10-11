# SquashMerge design language

Version: 1.0.0  
Date: 2026-10-07  
Scope: SquashMerge, Glasshouse, Trellis, Mimir and Cultivate coding projects.

Copy this file to a repository root and reference it from the project's agent instructions. Use it alongside the repository's architecture, component library, coding conventions and accessibility practices. Do not replace working infrastructure just to implement the brand.

The visual direction comes from the team's logo review. Exact tokens, typography, geometric ratios and interface conventions below are the proposed v1 implementation baseline. The PNG logos are design references, not mathematically normalized vector masters.

## 1. Product identity

| Brand | Role | Status and claims |
| --- | --- | --- |
| SquashMerge | Parent company and shared design language | One word; capital S and M. |
| Glasshouse | Observability and evaluations for agentic applications | Availability, integrations and pricing are not specified. |
| Trellis | Code intelligence and source-linked static diagrams | Commercial packaging is a hypothesis. |
| Mimir | Memory and general agent foundation | Free core; commercial plugins modeled separately. |
| Cultivate | Planned Mimir plugin | WorkLink → OpenCode → PR; Feature Factory epics, PR reviews and remediation. |

SquashMerge contains Glasshouse, Trellis and Mimir. Cultivate sits under Mimir. Do not confuse products, plugins and commercial plans. Do not invent prices, launch dates, supported integrations or performance claims.

First mentions: `Glasshouse by SquashMerge`, `Trellis by SquashMerge`, `Mimir by SquashMerge`. For roadmap copy: `Cultivate, a planned Mimir plugin by SquashMerge`. The logo endorsement `a Mimir plugin by SquashMerge` does not establish availability by itself.

## 2. Principles

1. **Make systems visible:** show state, evidence, source references and relationships.
2. **Make structure useful:** use hierarchy, alignment and spacing to explain the work.
3. **Support steady improvement:** make the next action clear and its result observable.
4. **Keep interfaces calm:** neutral surfaces dominate; accents carry meaning.
5. **Be warm and precise:** restrained botanical cues, geometric paths and deliberate negative space.

Avoid glossy 3D effects, neon glows, heavy shadows, decorative animated backgrounds, arbitrary product colors and unrelated icon styles. The garden metaphor belongs mainly in identity and occasional headlines, not technical instructions.

## 3. Assets and logo rules

The companion kit contains these final references:

| Brand | Kit path |
| --- | --- |
| SquashMerge | `logos/squashmerge-logo-dark-v1.png` |
| Glasshouse | `logos/glasshouse-logo-dark-v1.png` |
| Trellis | `logos/trellis-logo-dark-v1.png` |
| Mimir | `logos/mimir-logo-dark-v1.png` |
| Cultivate | `logos/cultivate-logo-dark-v1.png` |

Each is a 1254 × 1254 RGB PNG with a dark background. They are not transparent, editable SVGs or favicon exports. Preserve aspect ratio and the supplied artwork. Do not distort, recolor, filter, erase backgrounds, automatically trace or recreate wordmarks in a guessed font during routine interface work.

Resolve actual repository asset paths first. Kit paths are not a promise that files exist in a coding checkout. If assets are missing, use a clearly identified text-only product name temporarily and flag the missing artwork. Do not invent a replacement logo without a design task.

Use meaningful alt text when a logo is the only product label. When adjacent text already gives the same name, treat the image as decorative. A linked logo needs an accessible link name.

### Decisions to preserve

- **SquashMerge:** fuller butternut silhouette; bottom gap establishes the visual reference for crown and stem spacing.
- **Glasshouse:** exactly two interior nodes, one amber and one purple. No bottom purple ring; its curve flows smoothly into the base.
- **Trellis:** separate colored rails with clear over-and-under crossings. Foreground paths remain continuous and rear paths are interrupted.
- **Mimir:** a clean central channel separates the two halves of the shared memory ring.
- **Cultivate:** one closed purple leaf remains detached from the amber stem; the bottom shared ring has a clean central split.
- **All:** no amber-purple blending, touching seams, hairline gaps, cramped spacing or accidental tangencies. Maintain consistent circular nodes.

### Targets for dedicated vector production

These are targets, not measurements certified against the PNGs:

- Structural stroke width = `t`.
- Color-separation gap = `1t`, with reviewed optical adjustments for curves.
- Node outer diameter = `3t`; inner hole diameter = `1.3t`.
- Minimum outside clear space = `2t`, measured from visible artwork, not file edges.
- Mark-to-name gap = `1.5t`; name-to-endorsement gap = `1t` as starting points.
- Measure edge-to-edge spacing perpendicular to the closest contours.
- Use separate vector paths and true transparent knockouts, not painted black patches.
- Keep gradients within the warm or cool family; never interpolate one into the other.

Initial visible-artwork width targets: parent 220 px; ordinary endorsed products 260 px; Cultivate with its longer line 400 px. Review actual readability. Narrow spaces need separately reviewed responsive lockups. Do not squeeze square presentation images into navigation icons or favicons.

## 4. Color tokens

Use `tokens/squashmerge.tokens.json` and `tokens/squashmerge.css` when available. Define values once in the existing project token system; avoid scattered literals.

| Token | Hex | Purpose |
| --- | --- | --- |
| ink | `#0B0C10` | Page background; dark text on bright buttons |
| surface | `#14151D` | Panels and cards |
| raised | `#1D1E2A` | Raised groups and quiet navigation |
| line | `#343644` | Decorative divider; not sole control boundary |
| control | `#777A8E` | Meaningful control boundaries |
| white | `#F7F7FB` | Primary text |
| muted | `#B8BAC8` | Secondary text |
| gold | `#FFC20A` | Warm highlight and warning accent |
| amber | `#FFAA10` | Primary action and brand accent |
| orange | `#FF7A00` | Warm gradient endpoint |
| lavender | `#ABA5FF` | Links and focus on dark surfaces |
| purple | `#7770DC` | Selection and structural accent |
| deepPurple | `#5D55C8` | Cool gradient endpoint |
| green | `#57D6A0` | Success, with label or icon |
| red | `#FF8E9A` | Error, with label or icon |
| blue | `#85C7FF` | Information |

```css
:root {
  --sm-bg: #0B0C10;
  --sm-surface: #14151D;
  --sm-text: #F7F7FB;
  --sm-text-muted: #B8BAC8;
  --sm-action: #FFAA10;
  --sm-on-action: #0B0C10;
  --sm-selected: #7770DC;
  --sm-on-selected: #0B0C10;
  --sm-link: #ABA5FF;
  --sm-focus: #ABA5FF;
  --sm-control-border: #777A8E;
}
```

Use ink text on amber and the standard purple selected fill. White on these fills does not pass normal-text contrast with the specified tokens. Use lavender for links on dark surfaces and underline text links. Brand colors are not a universal success/failure mapping.

Warm gradient: `#FFC20A` to `#FF7A00`. Cool gradient: `#ABA5FF` to `#5D55C8`. Use vertical gradients in large brand artwork, not behind body copy. Light mode needs separately reviewed semantic aliases and contrast checks; do not invert dark mode mechanically.

## 5. Typography

| Role | Family | Weight |
| --- | --- | --- |
| Display and headings | Manrope | 600–800; display default 750 |
| Body and interface | Inter | 400 body, 600 labels, 650 emphasis |
| Code, IDs and metrics | JetBrains Mono | 400 |

Obtain the fonts from the official project links in `fonts/README.txt`; font binaries are not bundled. Preserve their OFL licenses when redistributing. Use the project's font-loading approach and `font-display: swap`. Until fonts are available, use `system-ui, sans-serif` and `monospace`; do not silently fetch unrelated families. The generated wordmarks do not have a confirmed source font and must not be retyped.

| Role | Size / line height |
| --- | --- |
| Display | 48 / 56 px |
| H1 | 32 / 40 px |
| H2 | 24 / 32 px |
| H3 | 18 / 26 px |
| Body | 16 / 24 px |
| Label | 14 / 20 px |
| Caption | 12 / 18 px |
| Code | 13 / 20 px |

Use sentence case, semantic headings, comfortable line height and restrained emphasis. Avoid tiny body text, all-caps passages and unexplained truncation. Use tabular numerals for aligned numbers.

## 6. Layout and shape

- Spacing: `4, 8, 12, 16, 24, 32, 48, 64 px`.
- Inside components: 8–16 px. Between groups: 24–32 px. Between sections: 48–64 px.
- Radius: 4 px details, 8 px controls, 12 px cards. Fully rounded shapes are reserved for real chips, avatar crops and status dots.
- Typical content width: 1120–1280 px where useful; data-heavy views can be wider for a reason.
- Mobile inset: 16–24 px. Collapse layout according to content. Avoid whole-page horizontal scrolling at 320 CSS px.
- Dividers: 1 px. Ordinary UI icon stroke: 2 px. Do not apply heavy logo strokes to all interface icons.
- Align related components, padding, control heights and labels consistently.
- Allow deliberate local overflow for code and wide tables, with accessible access to the full content.

## 7. Components and interaction

### Buttons

Primary buttons use amber fill, ink text, 44 px minimum height, 8 px radius, a 14/20 px label and roughly 10 px vertical / 16 px horizontal padding. Secondary actions use neutral surfaces and visible boundaries. Destructive actions use explicit wording and semantic error styling; preserve the product's established confirmation behavior.

Use real buttons for actions and links for navigation. Use specific labels such as `Run evaluation`, `Open source` and `Review changes`. Keep dimensions stable during loading. Prevent duplicate submissions where appropriate.

### Forms

Use persistent visible labels; placeholders are examples, not labels. Explain errors next to the affected field. Keep focus visible: a 2 px lavender ring with 3 px offset is the dark-surface starting point. Verify it in context. Reuse established accessible dialogs, menus, selects and validation components.

### Cards and technical data

Cards group related information; avoid a card around every sentence. Use semantic tables for comparable records, label units and align numeric data. Make sorting, selection and pagination understandable. Render paths and IDs in the mono family; expose full values through accessible expansion or copy behavior if shortened.

### Required states

- Empty: explain what belongs here and give the relevant next action.
- Loading: stable skeleton or meaningful progress; avoid layout jumps.
- Error: explain failure and recovery; do not imply success before a result.
- Disabled: communicate unavailability without relying only on faint color.
- Focus/hover/selected: visibly distinct, with keyboard operation preserved.

### Motion

Use 120 ms hover, 180 ms local state changes and 240 ms panel transitions, with `cubic-bezier(0.2, 0, 0, 1)`. Prefer opacity and small movement. Do not animate logos continuously. Respect `prefers-reduced-motion`; remove decorative movement and use immediate state changes when practical.

## 8. Diagrams, evaluations and status

- Make graph relationships functional, not decorative. Use arrows when direction matters and avoid ambiguous crossings.
- Prefer short orthogonal connectors. Keep source references close to the item they explain.
- Label observed, inferred and planned relationships. Never fabricate source links, evaluations, trace IDs or results.
- Use consistent units and scales. Mark illustrative or synthetic data clearly.
- Status needs words plus shape/icon or line style, not color alone.
- Do not use ornamental logo overlaps if they confuse actual diagram semantics.
- Glasshouse emphasizes visibility; Trellis source structure; Mimir memory/foundation; Cultivate development workflow. These differences guide content, not independent color schemes.

## 9. Voice

Be precise, calm, capable and warm. Name the action, object and evidence. Explain limitations plainly.

Working company description: `SquashMerge builds tools for understanding, evaluating and developing agentic applications.`

Prefer: `Inspect the trace and compare evaluation results.`  
Avoid: `Unlock revolutionary AI insights.`

Keep technical terms concrete: evaluations, source-linked diagrams, memory, pull requests. Do not introduce unsupported superlatives, pricing, availability or integration promises.

## 10. Accessibility and verification

- Target 4.5:1 contrast for normal text and 3:1 for large text under the applicable definition. Meaningful UI graphics and relevant control boundaries need 3:1 under the applicable criterion.
- `tokens/contrast-report.json` covers selected flat pairs, not a whole application. Check actual surfaces, gradients, focus and interaction states.
- Preserve keyboard access, correct roles, accessible names and visible focus. Reuse the repository's accessible interaction patterns.
- Do not rely on color, hover alone or animation to communicate required information.
- Check the affected flow at narrow and wide widths, using realistic long content and all important states.
- Reuse relevant project lint, type-check, build and test commands. Add tests when behavior or regression risk warrants them; do not create tests that merely repeat static styling.
- Visually inspect the actual rendered change before declaring it complete. Scope verification to the changed flow and material risks.

Primary references: [WCAG text contrast](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html) and [WCAG non-text contrast](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html).

## 11. Coding-agent workflow

1. Identify the current product, requested scope, styling system and existing components.
2. Read this file and companion tokens when available. Resolve real repository asset paths.
3. Implement the requested scope using shared defaults; avoid unrelated restyling.
4. Preserve product status and claims. Get product direction when an unconfirmed commercial decision is necessary.
5. Inspect the result and run focused checks. Fix overflow, contrast, state handling and inconsistent spacing.
6. Report what changed, how it was verified and any material limitation.

For a deliberate brand-system change, update this file and corresponding tokens together. Version the change and identify any remaining old assets or screens. Do not claim a whole-product update when only one flow changed.

## 12. Companion files

- `SquashMerge-Brand-Guide-v1.0.pdf`: illustrated guide and examples.
- `tokens/squashmerge.tokens.json`: structured values; `{...}` aliases require tooling resolution.
- `tokens/squashmerge.css`: CSS variables, font declarations and starter components.
- `docs/decisions-and-status.txt`: recorded feedback and production status.
- `docs/asset-manifest.json`: artwork inventory and checksums.
- `fonts/README.txt`: official font sources and licensing guidance.

This specification is portable. Preserve the distinction between established references and proposed production defaults when handing it to other agents or collaborators.
