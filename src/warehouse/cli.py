"""halcyon-warehouse: one entry point for the data pipeline.

    halcyon-warehouse download [--source kaggle|github-mirror]
    halcyon-warehouse sample [--orders 4000]
    halcyon-warehouse build
"""
from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in {"download", "sample", "build"}:
        sys.exit(__doc__)
    cmd = sys.argv.pop(1)
    if cmd == "download":
        from warehouse.download import main as run
    elif cmd == "sample":
        from warehouse.sample import main as run
    else:
        from warehouse.build import main as run
    run()


if __name__ == "__main__":
    main()
