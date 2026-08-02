"""命令行分析入口:  python run_analyze.py 600519"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.cli.analyze import main

if __name__ == "__main__":
    sys.exit(main())
