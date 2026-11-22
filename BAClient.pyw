"""Double-click to open the BAClient HUD (runs without a console window)."""
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

from ba_auto.client import main  # noqa: E402

main()
