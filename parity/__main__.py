"""Parity: snapshot a computer's programs and hardware before a reinstall.

    parity            scan this computer   (or: python -m parity)
    parity --demo     try the UI with sample data
    parity --smoke    open, run a demo scan, quit (exit code 0 = it works)
"""
import sys


def main() -> int:
    from parity.ui import run
    return run(demo="--demo" in sys.argv, smoke="--smoke" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
