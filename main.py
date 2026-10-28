"""Entry script for PyInstaller builds; everyone else can use `parity` or `python -m parity`."""
import sys

from parity.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
