"""
run_pipeline.py
=================

Flags:
    --skip-external   Skip steps 3-6 (freight + scraper + price-gap). Useful
                       for quickly re-testing the core dashboard without
                       waiting on the external API/scraper (freight pull
                       alone takes a few minutes). The app will fall back to
                       proxy metrics for anything skipped, same as if those
                       scripts had never been run.
    --data-only       Run steps 1-7 (build everything) but don't launch
                       Streamlit at the end.
    --no-cache        Passed through to build_freight_mart.py -- forces a
                       fresh pull from the carrier API instead of using the
                       cached invoices from a previous run.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

import requests

PYTHON = sys.executable


def log(msg: str) -> None:
    print(f"[run_pipeline] {msg}")


def wait_for(url: str, timeout: float) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(url, timeout=2)
            if resp.status_code < 500:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


def ensure_server(name: str, health_url: str, cmd: list[str],
                   cwd: str = None, log_file: str = None):
    """Starts a background server."""
    if wait_for(health_url, timeout=2):
        log(f"{name}: already running at {health_url}, reusing it.")
        return None

    log(f"{name}: not running, starting it in the background "
        f"(output -> {log_file})...")
    log_path = Path(log_file)
    f = open(log_path, "w")
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT)

    if wait_for(health_url, timeout=20):
        log(f"{name}: up and responding.")
        return proc

    log(f"{name}: ERROR -- did not become ready within 20s. "
        f"Check {log_path.resolve()} for details.")
    proc.terminate()
    sys.exit(1)


def run_step(name: str, cmd: list[str]) -> None:
    log(f"Running {name} ...")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        log(f"ERROR: {name} failed (exit code {result.returncode}). "
            f"Stopping the pipeline here -- downstream steps assume this "
            f"one succeeded.")
        sys.exit(result.returncode)
    log(f"{name}: done.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-external", action="store_true",
                         help="Skip the freight API pull, scraper, and price-gap "
                              "matching. Runs only the core marts build.")
    parser.add_argument("--data-only", action="store_true",
                         help="Build all data but don't launch Streamlit at the end.")
    parser.add_argument("--no-cache", action="store_true",
                         help="Force a fresh freight invoice pull instead of using "
                              "the cached copy from a previous run.")
    args = parser.parse_args()

    api_proc = None
    site_proc = None

    run_step("build_marts.py", [PYTHON, "build_marts.py"])

    if not args.skip_external:
        api_proc = ensure_server(
            "partner_api", "http://localhost:8088/v1/health",
            [PYTHON, "partner_api/server.py"], log_file="partner_api.log",
        )
        freight_cmd = [PYTHON, "build_freight_mart.py"]
        if args.no_cache:
            freight_cmd.append("--no-cache")
        run_step("build_freight_mart.py", freight_cmd)

        site_proc = ensure_server(
            "bazaarpulse_site", "http://localhost:8080/",
            [PYTHON, "-m", "http.server", "8080"],
            cwd="bazaarpulse_site", log_file="bazaarpulse_site.log",
        )
        run_step("build_bazaarpulse_mart.py", [PYTHON, "build_bazaarpulse_mart.py"])
        run_step("build_price_gap_mart.py", [PYTHON, "build_price_gap_mart.py"])
    else:
        log("--skip-external: skipping freight/scraper/price-gap. The app will "
            "show proxy/fallback metrics for those sections.")

    for proc, name in [(api_proc, "partner_api"), (site_proc, "bazaarpulse_site")]:
        if proc is not None:
            log(f"Stopping {name} (no longer needed -- its data has been pulled).")
            proc.terminate()

    log("Pipeline complete.")

    if args.data_only:
        log("--data-only set: not launching Streamlit. Run "
            "`streamlit run app.py` when you're ready to view the dashboard.")
        return

    log("Launching dashboard: streamlit run app.py")
    subprocess.run([PYTHON, "-m", "streamlit", "run", "app.py"])


if __name__ == "__main__":
    main()
