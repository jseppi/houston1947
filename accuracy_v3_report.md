# Accuracy assessment (group level, area-weighted; Olofsson et al. 2014)

Sample: 270 points, {'R': 90, 'C': 90, 'I': 90} per predicted stratum; weights W_h = {R: 0.663, C: 0.083, I: 0.254}

## Error matrix (estimated area proportions; rows = map, cols = reference)

| map \ ref | R | C | I | N | W_h | n_h |
|---|---|---|---|---|---|---|
| R | 0.6110 | 0.0147 | 0.0221 | 0.0147 | 0.663 | 90 |
| C | 0.0241 | 0.0352 | 0.0195 | 0.0046 | 0.083 | 90 |
| I | 0.0254 | 0.0028 | 0.2173 | 0.0085 | 0.254 | 90 |
| total | 0.6605 | 0.0528 | 0.2589 | 0.0278 | 1 | |

**Overall accuracy**: 0.864 ± 0.042 (95% CI)

| group | user's acc. | ± | producer's acc. | ± | mapped share | error-adjusted share | ± |
|---|---|---|---|---|---|---|---|
| R | 0.922 | 0.056 | 0.925 | 0.025 | 0.663 | 0.661 | 0.041 |
| C | 0.422 | 0.103 | 0.668 | 0.271 | 0.083 | 0.053 | 0.023 |
| I | 0.856 | 0.073 | 0.839 | 0.084 | 0.254 | 0.259 | 0.032 |

Reference 'N' (no zone) share of mapped zone area: 0.028

## District-level confusion (sample counts; rows = predicted, cols = reference)

| pred \ ref | A | B | C | D | E | F | G | H | I | J | 0 | n | UA |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 34 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 35 | 0.97 |
| B | 2 | 33 | 1 | 2 | 0 | 0 | 0 | 1 | 0 | 0 | 2 | 41 | 0.80 |
| C | 0 | 0 | 9 | 1 | 1 | 1 | 0 | 2 | 0 | 0 | 0 | 14 | 0.64 |
| D | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | – |
| E | 2 | 5 | 4 | 12 | 21 | 3 | 0 | 9 | 0 | 1 | 3 | 60 | 0.35 |
| F | 0 | 1 | 2 | 0 | 6 | 7 | 0 | 6 | 0 | 5 | 1 | 28 | 0.25 |
| G | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 1 | 2 | 0.50 |
| H | 0 | 1 | 3 | 3 | 0 | 0 | 0 | 25 | 2 | 0 | 2 | 36 | 0.69 |
| I | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 4 | 1 | 0 | 9 | 0.44 |
| J | 0 | 0 | 0 | 2 | 1 | 0 | 0 | 0 | 0 | 41 | 1 | 45 | 0.91 |

## Match rates corrected for misclassification (stratified ratio estimator)

Match = 2026 parcel use in the strict/cumulative set of the point's **reference** (true) 1947 district; denominator = points on non-neutral parcel land in that true group. The same estimator on **mapped** labels is shown for comparison with the wall-to-wall overlay.

| 1947 group | strict (true) | 95% CI | cumulative (true) | 95% CI | n points | strict (mapped labels, same sample) |
|---|---|---|---|---|---|---|
| Residential | 60.4% | [48.5, 72.2] | 78.1% | [68.1, 88.1] | 79 | 61.9% |
| Commercial | 37.3% | [14.2, 60.4] | 86.0% | [73.6, 98.4] | 27 | 48.1% |
| Industrial | 52.9% | [39.2, 66.6] | 100.0% | [100.0, 100.0] | 60 | 51.9% |
