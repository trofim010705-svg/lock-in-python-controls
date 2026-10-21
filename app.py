#!/usr/bin/env python3
"""Pump–Probe Lab. Run `python app.py --demo` for a hardware-free preview."""
import argparse
import sys
from PyQt5.QtWidgets import QApplication
from pump_probe.ui import ExperimentWindow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true', help='Симуляция локина и линии задержки')
    parser.add_argument('--serial', help='Серийный номер USB локина')
    parser.add_argument('--timeout', type=int, default=2000, help='Тайм-аут VISA, ms')
    parser.add_argument('--max-frequency', type=int, choices=(120000, 250000), default=120000)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout должен быть положительным')
    app = QApplication(sys.argv[:1]); app.setStyle('Fusion')
    window = ExperimentWindow(args); window.show()
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
