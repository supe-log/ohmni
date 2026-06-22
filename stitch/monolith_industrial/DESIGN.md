```markdown
# Design System Document: The Monolithic Interface

## 1. Overview & Creative North Star: "The Brutalist Precision"
The North Star of this design system is **Brutalist Precision**. Unlike consumer-grade apps that rely on soft shadows and friendly curves, this system draws inspiration from mid-century laboratory equipment and aerospace telemetry panels. It is a high-functioning, "zero-empathy" interface designed for the serious task of robotics control.

We move beyond "standard" UI by embracing a strictly orthogonal, hard-edged aesthetic. There are no rounded corners, no glows, and no decorative gradients. The "premium" feel is derived from perfect alignment, generous whitespace, and the authoritative use of high-contrast typography. It feels intentional, expensive, and engineered—never "default."

## 2. Colors: Tonal Architecture
The palette is a disciplined study in grayscale. Because we are stripping away color, the "weight" of each gray becomes a functional tool for navigation.

### The "No-Line" Rule
Traditional dividers are forbidden. Instead, define structural boundaries through background shifts. A section should be distinguished from the main canvas by moving from `surface` (#131313) to `surface_container_low` (#1b1b1b). Use `surface_container_highest` (#353535) only for the most critical interactive zones.

### Surface Hierarchy & Nesting
Treat the UI as a machined block of material. 
- **Base Layer:** `surface` (#131313) for the global background.
- **Primary Modules:** `surface_container` (#1f1f1f).
- **Nested Controls:** `surface_container_high` (#2a2a2a) for specific control groups within a module.
- **Action States:** Use `primary` (#ffffff) for active states to create a stark, undeniable visual punch.

### Signature Textures
While we avoid "glows," we utilize "Active Fills." A primary CTA should use a solid `primary` (#ffffff) fill with `on_primary` (#1a1c1c) text. To signify a "ready" or "standby" state, use a subtle linear gradient between `surface_container_highest` and `surface_bright` to give the element a tactile, metallic density.

## 3. Typography: The Information Hierarchy
We utilize **Inter** (as the refined evolution of the Roboto-style functionalist font) to provide a clean, technical atmosphere. Typography here is not just for reading; it is a graphical element.

- **Display & Headline:** Used for telemetry data and primary mode titles. These should be tracked out slightly (+2% to +4%) to enhance the "instrumental" feel.
- **Title & Body:** Focused on high legibility. Use `on_surface` (#e2e2e2) for primary data and `on_surface_variant` (#c6c6c6) for secondary labels.
- **Labels:** Small, all-caps labels are the backbone of this system. Use `label-sm` with `outline` (#919191) for units of measurement (e.g., "RPM," "VELOCITY").

## 4. Elevation & Depth: The Stacking Principle
In this system, depth is not achieved through light and shadow, but through **Tonal Layering** and **Ghost Borders**.

- **The Layering Principle:** To "lift" a component, shift its background color one step higher in the surface-container scale. A card doesn't "float" above the background; it is "milled" out of it.
- **Ambient Shadows:** Standard drop shadows are prohibited. If a temporary overlay (like a critical modal) is required, use a 0px offset shadow with a 40px blur at 15% opacity, using the `surface_container_lowest` color to create a "void" effect rather than a shadow.
- **The "Ghost Border":** For high-density data grids where tonal shifts are insufficient, use a 1px border using `outline_variant` (#474747). This provides structure without breaking the high-contrast minimalist aesthetic.

## 5. Components

### Buttons
- **Primary:** Solid `primary` (#ffffff) background, `on_primary` (#1a1c1c) text. Hard 0px corners.
- **Secondary:** Transparent background with a 1px `outline` (#919191) border. 
- **Tertiary/Ghost:** No border, `on_surface` text. Becomes `surface_container_highest` on hover.

### Inputs & Fields
- **Default State:** `surface_container_low` background with a bottom-only border of `outline_variant`.
- **Active State:** 1px solid `primary` (#ffffff) border around the entire perimeter.
- **Error State:** Use the `error` (#ffb4ab) token for the border and helper text.

### Instrument Cards
- **Construction:** Use `surface_container` as the base. 
- **Header:** A 4px vertical accent bar of `primary` on the far left of the card title to denote the "Active" module.
- **Data Points:** Large `headline-lg` numerals for values, paired with `label-sm` for descriptions.

### Status Indicators (The "Non-Neon" Rule)
Instead of glowing LEDs, use high-contrast fills.
- **Active:** Solid `primary` (#ffffff).
- **Inactive:** `surface_container_highest` (#353535).
- **Critical:** Solid `error` (#ffb4ab) with a blinking animation (no blur/glow).

## 6. Do's and Don'ts

### Do:
- **Do** use intentional asymmetry. Group critical controls on a larger surface area and secondary telemetry in a narrow side column.
- **Do** use strict 8px/16px/24px spacing increments to maintain the "engineered" feel.
- **Do** use "Ghost Borders" at low opacity to separate data rows in high-density tables.

### Don't:
- **Don't** use any border-radius. Every corner must be a sharp 90-degree angle (0px).
- **Don't** use drop shadows to indicate hierarchy; use tonal shifts between `surface_container` tiers.
- **Don't** use color for anything other than "Error" or "Warning" states. The interface must remain strictly monochromatic to ensure user focus.
- **Don't** use divider lines to separate list items; use vertical whitespace or a 1-step tonal shift on hover.```