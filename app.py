#!/usr/bin/env python3
"""Signal Recovery 7230: USB controls and timed Magn1 / Phase1 histograms."""
import argparse
import sys
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication
from measurement.ui import MeasurementWindow


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo',action='store_true',help='Условные показания без USB и VISA-драйвера')
    parser.add_argument('--serial',help='Серийный номер USB локина')
    parser.add_argument('--timeout',type=int,default=2000,help='USB timeout, ms')
    parser.add_argument('--max-frequency',type=int,choices=(120000,250000),default=120000)
    args=parser.parse_args()
    if args.timeout<=0:parser.error('--timeout должен быть положительным')
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling,True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps,True)
    app=QApplication(sys.argv[:1]);app.setStyle('Fusion')
    window=MeasurementWindow(args);window.show()
    return app.exec_()


if __name__=='__main__':sys.exit(main())
