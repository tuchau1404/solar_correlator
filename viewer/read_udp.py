#!/usr/bin/env python3
"""Console monitor sharing the exact decoder used by the GUI."""
import argparse
import signal
import time
import numpy as np
from telemetry_protocol import Receiver


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=9999)
    parser.add_argument('--source-ip')
    args = parser.parse_args()
    receiver = Receiver(args.bind, args.port, args.source_ip)
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    receiver.start()
    try:
        while running:
            frames, stats, skips, error = receiver.take()
            for session, frame, stamp, count, values in frames:
                peak = int(np.argmax(values))
                print(f'session={session:016x} frame={frame} UTC_us={stamp} M={count} '
                      f'peak={values[peak]:.2f} dB @ {30 + peak * 10 / 2048:.6f} MHz '
                      f'stats={stats} console_skips={skips}')
            if error:
                raise RuntimeError(error)
            time.sleep(0.05)
    finally:
        receiver.close()


if __name__ == '__main__':
    main()
