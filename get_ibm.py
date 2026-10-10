"""
Quick sanity check that all installed packages work.
Run with: python3 test_packages.py
"""

"""
Pull historical calibration data from IBM Quantum backends.

Config:
  BACKENDS         - list of backend names to query
  DAYS_BACK        - how many days of history to pull
  INTERVAL_HOURS   - sampling interval within the window

Uses backend.properties(datetime=<past_datetime>) to fetch the calibration
snapshot that was active at each past timestamp. Because IBM typically
recalibrates only every ~24 hours, most 6-hour queries within the same day
return the same snapshot — we deduplicate by (backend, last_update_date)
so the final CSV has one row per (unique snapshot × qubit).

Output:
  ibm_calibration_history.csv    - long-format tidy dataframe

Run with: python3 pull_ibm_history.py
"""

from datetime import datetime, timedelta, timezone
import pandas as pd
from qiskit_ibm_runtime import QiskitRuntimeService

# --- Config -----------------------------------------------------------------
BACKENDS = ["ibm_fez", "ibm_marrakesh", "ibm_kingston"]
DAYS_BACK = 180
INTERVAL_HOURS = 6
OUTPUT_CSV = "ibm_calibration_history.csv"
CHECKPOINT_EVERY = 20  # save partial CSV every N snapshots, in case of crash
# ----------------------------------------------------------------------------


def extract_qubit_rows(properties, backend_name):
    """
    Turn a BackendProperties object into a list of per-qubit dict rows.
    Uses backend.properties().to_dict() and extracts the fields most useful
    for noise/drift analysis.
    """
    props_dict = properties.to_dict()
    snapshot_time = props_dict["last_update_date"]

    rows = []
    for qubit_idx, qubit_data in enumerate(props_dict["qubits"]):
        row = {
            "backend": backend_name,
            "snapshot_time": snapshot_time,
            "qubit": qubit_idx,
        }
        # Each qubit_data is a list of {name, date, unit, value} dicts (Nduv)
        for item in qubit_data:
            row[item["name"]] = item["value"]
            row[item["name"] + "_unit"] = item["unit"]
        rows.append(row)

    return rows


def main():
    print("Connecting to IBM Quantum...")
    service = QiskitRuntimeService()

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=DAYS_BACK)
    timestamps = []
    t = start
    while t <= now:
        timestamps.append(t)
        t += timedelta(hours=INTERVAL_HOURS)

    print(f"Will query {len(BACKENDS)} backends × {len(timestamps)} timestamps "
          f"= {len(BACKENDS) * len(timestamps)} requests total.\n")

    all_rows = []
    # Track which (backend, snapshot_time) combos we've already stored, to
    # dedupe when consecutive 6h queries return the same calibration.
    seen = set()
    request_count = 0

    for backend_name in BACKENDS:
        print(f"\n=== {backend_name} ===")
        try:
            backend = service.backend(backend_name)
        except Exception as e:
            print(f"  Could not access backend: {e}")
            continue

        unique_snapshots_this_backend = 0

        for i, ts in enumerate(timestamps):
            request_count += 1
            try:
                props = backend.properties(datetime=ts)
                if props is None:
                    continue
                snap_time = props.to_dict()["last_update_date"]
                key = (backend_name, str(snap_time))
                if key in seen:
                    continue
                seen.add(key)
                all_rows.extend(extract_qubit_rows(props, backend_name))
                unique_snapshots_this_backend += 1
            except Exception as e:
                # Some datetimes far in the past may have no data
                print(f"  [{ts.date()}] failed: {e}")
                continue

            # Progress + checkpoint
            if (i + 1) % 10 == 0:
                print(f"  progress: {i+1}/{len(timestamps)} timestamps queried, "
                      f"{unique_snapshots_this_backend} unique snapshots so far")

            if request_count % CHECKPOINT_EVERY == 0 and all_rows:
                pd.DataFrame(all_rows).to_csv(OUTPUT_CSV, index=False)

        print(f"  → {unique_snapshots_this_backend} unique snapshots collected "
              f"for {backend_name}")

    if not all_rows:
        print("\nNo data collected. Check your IBM connection and try again.")
        return

    df = pd.DataFrame(all_rows)
    df.to_csv(OUTPUT_CSV, index=False)

    print(f"\n{'=' * 60}")
    print(f"Done. Saved {len(df):,} rows to {OUTPUT_CSV}")
    print(f"  Unique snapshots: {df.groupby('backend')['snapshot_time'].nunique().to_dict()}")
    print(f"  Date range: {df['snapshot_time'].min()} → {df['snapshot_time'].max()}")
    print(f"  Columns: {list(df.columns)}")


if __name__ == "__main__":
    main()
