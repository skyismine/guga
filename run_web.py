"""启动 Web 仪表盘:  python run_web.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web.server import main

if __name__ == "__main__":
    main()
