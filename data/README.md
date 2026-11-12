# Hand-fed player files

`pool_nba.csv` and `pool_mls.csv` are the player lists for the two sports with no free feed. Columns:

    src,ext,name,sport,position,score

- `src`: any short label for where the list came from (`nba`, `mls`); `ext`: any unique id (the name is fine).
- `sport`: `NBA` or `MLS`. `position`: NBA Big / Guard / Wing; MLS Defender / Forward / Goalkeeper / Midfielder.
- `score`: how good the player is, as a number where 0 is an average starter and 2 is a star (negative is below average). Or leave `score` empty and put `ranking` (1 = best in that sport) instead.

Then run the "Load players" workflow (preview first).
