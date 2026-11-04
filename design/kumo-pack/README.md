# Kumo 2D — Production Starter Pack

A first editable vector master for the Kumo cloud mascot. All artwork is original SVG authored as flat shapes.

## Included
- `svg/kumo_master.svg` — front-facing master character
- `svg/kumo_side.svg` and `svg/kumo_back.svg` — side and rear model views
- `svg/kumo_symbol.svg` — simplified cloud symbol on transparent background
- `svg/kumo_app_icon.svg` — square app icon (square corners; OS applies adaptive mask)
- `svg/stickers/*.svg` — 12 editable expression/action stickers
- `png/*.png` and `png/stickers/*.png` — 1024×1024 transparent PNG exports
- `kumo_sticker_contact_sheet.jpg` and `kumo_turnaround_preview.jpg` — visual previews

## Brand palette
- Cloud white: `#F8FAFF`
- Signature blue: `#168BFA`
- Deep indigo outline: `#202941`
- Scarf navy: `#172447`
- Scarf highlight: `#293B68`
- Pale blue: `#DCEBFF`
- Sakura blush: `#F4AFC8`

## Character invariants
1. Rounded cloud silhouette with a slightly hand-drawn, bold outline.
2. One electric-blue curl on the upper-right side of the head.
3. Deep-indigo scarf with a small five-petal sakura emblem.
4. Minimal dark face: expressive eyes and a small mouth.
5. Flat fills only; no gradients, glossy 3D highlights, or realistic texture.

## Usage
Use SVG for UI scaling and future animation. Use transparent PNG where raster assets are required. Keep the blue curl and cloud silhouette consistent across every pose. App icon artwork has square corners by design; apply platform adaptive icon masks at implementation time.

## Notes
This is a vector starter system and a first-pass model sheet, not a fully rigged animation file. Side/back geometry is an art-direction reference; refine those views together with the final approved master before commissioning or implementing a production animation rig.
