"""
Entry point: runs the full Steps 1-6 pipeline across every symbol in
./crypto csv data and writes the per-symbol outputs to ./short_research_output/.
"""
import pickle
import time

from short_research import pipeline

if __name__ == "__main__":
    t0 = time.time()
    records = pipeline.run_all()
    print(f"Analyzed {len(records)} symbols in {time.time() - t0:.1f}s")

    config_df = pipeline.to_config_df(records)
    full_df = pipeline.to_full_df(records)

    import os
    os.makedirs("short_research_output", exist_ok=True)
    config_df.to_csv("short_research_output/per_symbol_config.csv", index=False)
    full_df.to_csv("short_research_output/per_symbol_full.csv", index=False)
    with open("short_research_output/records.pkl", "wb") as f:
        pickle.dump(records, f)

    print(config_df["edge_class"].value_counts())
    print("Wrote short_research_output/{per_symbol_config.csv, per_symbol_full.csv, records.pkl}")
