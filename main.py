"""Parity: snapshot a computer's programs and hardware before a reinstall.

    python main.py          scan this computer
    python main.py --demo   try the UI with sample data
    python main.py --smoke  open, run a demo scan, quit (exit code 0 = it works)
"""
import sys

from parity.ui import run

if __name__ == "__main__":
    sys.exit(run(demo="--demo" in sys.argv, smoke="--smoke" in sys.argv))
