#!/usr/bin/env python3
"""Bounded startup-only retries. Requires alignment_test with exit code 75.
No USB/service reset is added here; the executable owns hardware initialization.
"""
import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path


def run(binary, runs):
    for attempt in range(1, runs + 1):
        print(f"[AUTO-RUN] Launch {attempt}/{runs}", flush=True)
        # Same foreground process group: terminal Ctrl+C reaches parent and SDR
        # children, and the executable retains its existing cleanup protocol.
        child = subprocess.Popen([str(binary)])
        try:
            code = child.wait()
        except KeyboardInterrupt:
            # The foreground process group already received SIGINT. Also send
            # it to the parent for callers that delivered SIGINT only to us.
            try:
                child.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            print("[AUTO-RUN] Interrupted; waiting for hardware cleanup. No restart.", flush=True)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            child.wait()
            return 130
        if code != 75:
            return code if code >= 0 else 128 - code
        if attempt < runs:
            print("[AUTO-RUN] Startup failed; previous process exited. Retrying in 1 s.", flush=True)
            time.sleep(1)
    print("[AUTO-RUN] Retry limit reached; alignment was NOT accepted.", flush=True)
    return 75


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path,
                        default=Path(__file__).resolve().parents[1] / "build/alignment_test")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be >= 1")
    try:
        return run(args.binary.resolve(), args.runs)
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"[AUTO-RUN] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
