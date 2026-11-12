"""Export the scoring settings from the Excel workbook into engine/config.json.

Run:  python3 -m engine.export_config path/to/fantasy_scoring_model.xlsx
The workbook stays the place to tune weights; this keeps the engine in sync with it.
"""
import json
import sys
from pathlib import Path
from openpyxl import load_workbook


def export(xlsx, out):
    wb = load_workbook(xlsx, data_only=True)
    st, npw, wl, bs = wb["Settings"], wb["NativePoints"], wb["WeightLookup"], wb["BonusSetup"]

    weights = {}
    for r in range(2, wl.max_row + 1):
        k, w = wl.cell(row=r, column=1).value, wl.cell(row=r, column=2).value
        if k:
            weights[k] = w

    baselines, min_games = {}, {}
    for r in range(17, 34):
        key = f"{st.cell(row=r, column=1).value}|{st.cell(row=r, column=2).value}"
        baselines[key] = [st.cell(row=r, column=3).value, st.cell(row=r, column=4).value]
        min_games[key] = st.cell(row=r, column=13).value

    race_points = {str(npw.cell(row=r, column=6).value): npw.cell(row=r, column=7).value for r in range(87, 107)}

    cfg = {
        "scale": st["B4"].value,
        "floor": st["B5"].value,
        "variety_per_sport": st["B6"].value,
        "variety_cap": st["B7"].value,
        "starter_slots": st["B8"].value,
        "bench_slots": st["B9"].value,
        "ir_slots": st["B10"].value,
        "max_f1_per_team": 20,        # the workbook still says 2; the commissioner removed the limit (20 = a full roster)
        "playoff_by_record": st["B12"].value,
        "playoff_wildcards": st["B13"].value,
        "min_pool_size": st["B14"].value,
        "baseline_weeks": 4,
        "weights": weights,
        "baselines_fallback": baselines,
        "min_games": min_games,
        "f1": {
            "race_points": race_points,
            "pole_bonus": npw["B92"].value,
            "p2_p3_bonus": npw["B93"].value,
            "sprint_points": [8, 7, 6, 5, 4, 3, 2, 1],
        },
        "bonus_bands": [[bs.cell(row=r, column=1).value, bs.cell(row=r, column=2).value] for r in range(6, 13)],
        "bonus_statuses_scoring_zero": ["Missed cut", "Withdrew", "Did not start", "Not classified"],
    }
    Path(out).write_text(json.dumps(cfg, indent=2))
    return cfg


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "fantasy_scoring_model.xlsx"
    export(src, Path(__file__).with_name("config.json"))
    print("wrote config.json")
