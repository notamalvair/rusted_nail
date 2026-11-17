import argparse
import datetime as dt
import importlib.util
import json
import pathlib
import shutil
import time

import duckdb


script = pathlib.Path(__file__).with_name("resume_quintile_catalog_fast.py")
spec = importlib.util.spec_from_file_location("resume", script)
resume = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resume)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("collection", type=pathlib.Path)
    args = parser.parse_args()
    output = args.collection / "QUINTILES"
    database = output / "metric_catalog.duckdb"
    status_path = output / "RUN_STATUS.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    connection = duckdb.connect(str(database))
    started = time.monotonic()
    try:
        connection.execute("SET threads=1")
        connection.execute("SET memory_limit='384MB'")
        report = resume.finish(connection, output, started)
        status.update(status="passed", completed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                      threshold_rows=report["threshold_rows"], eligible_threshold_rows=report["eligible_threshold_rows"],
                      compact_repair="clamped_approx_quantiles_to_observed_min_max",
                      finalization_seconds=report["elapsed_seconds_resume"])
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    except Exception as error:
        status.update(status="failed", error_type=type(error).__name__, error=str(error),
                      failed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
        raise
    finally:
        connection.close()
    print(json.dumps({"phase": "catalog_ready", "threshold_rows": report["threshold_rows"],
                      "eligible_threshold_rows": report["eligible_threshold_rows"],
                      "finalization_seconds": report["elapsed_seconds_resume"], "files": report["files"]},
                     ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
