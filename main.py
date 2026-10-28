"""Parity: snapshot a Windows PC's programs and hardware before a reinstall.

    python main.py          scan this PC
    python main.py --demo   try the UI with sample data (works on any OS)
"""
import sys

from parity.ui import run

if __name__ == "__main__":
    sys.exit(run(demo="--demo" in sys.argv))
