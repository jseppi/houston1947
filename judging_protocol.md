# Blind judging protocol (reference data for accuracy assessment)

- **Unit:** a 180×180 px tile of the full-resolution scan (≈1,190 ft square, at ~0.152 px/ft), centred on the sample
  point, with a 25 px red box marking the point.
- **Label:** the district whose screen pattern fills the block under the box.
  - If the box falls on a street, bayou or blank paper, label **0**.
  - If it straddles two fills, use the fill under the box centre.
- **Blindness:** the predicted class is never displayed. Tile order is randomised with a fixed seed. The legend swatches
  are shown at the bottom of each sheet (`swatch_zoom.png` for close-ups).
- **Decision rules** (from `legend.png` and `swatch_zoom.png`):

  | District | Screen |
  |---|---|
  | A | Fine dark stipple on cream; the lightest fill |
  | B | Descending (\\) lines with small bow-tie marks; light |
  | C | Coarse lattice: steep rising (/) dark bars enclosing light parallelogram cells; medium |
  | D | Vertical ladder: vertical dark lines with slanted cross-ties |
  | E | Dense dark crosshatch of small square cells |
  | F | Dark diamond crosshatch; larger, more open cells than E |
  | G | Solid near-black, no texture |
  | H | Fine uniform ~45° rising (/) lines, equal dark and light widths, no cells |
  | I | Dark ground with thin light descending (\\) lines; darker than H |
  | J | Dark ground with a regular grid of white dots |

- **Rater:** a single rater (the analyst, using an AI vision model). Intra-rater repeatability was measured on a shuffled
  30-tile subset: district agreement 25/30 (83%), group agreement 27/30 (90%), group-level Cohen's κ = 0.85.
  Disagreements were confined to the diagonal-hatch districts (B/C/H/I). Those judgments were made before this written
  protocol existed. See `rater_repeat_result.csv`.
