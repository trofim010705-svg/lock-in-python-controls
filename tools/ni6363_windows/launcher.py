"""Windows entry point and a hardware-free frozen application check."""
import json
import platform
from pathlib import Path
import sys
import traceback


def smoke_test(report_path):
    import numpy as np
    from PyQt5.QtCore import Qt, QTimer, QT_VERSION_STR, PYQT_VERSION_STR
    from PyQt5.QtWidgets import QApplication
    from backend import load_daqmx, discover_devices
    from core import export_snapshot
    from gui import MainWindow

    report = Path(report_path)
    report.parent.mkdir(parents=True, exist_ok=True)
    outcome = {'ok': False, 'frozen': bool(getattr(sys, 'frozen', False)),
               'platform': platform.platform(), 'python': platform.python_version(),
               'qt': QT_VERSION_STR, 'pyqt': PYQT_VERSION_STR}
    api, constants, *_ = load_daqmx()
    assert constants.TerminalConfiguration.DIFF.value == 10106
    outcome['ni_python_api_imported'] = True
    try:
        devices, version = discover_devices()
        outcome['ni_driver'] = version
        outcome['ni_device_count'] = len(devices)
    except Exception as exc:
        if type(exc).__name__ != 'DaqNotFoundError':
            raise
        outcome['ni_driver'] = 'not installed; expected and handled'
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv[:1])
    app.setApplicationName('NIUSB6363Reader-BuildCheck')
    app.setOrganizationName('LabTools')
    app.setStyle('Fusion')
    window = MainWindow(demo=True)
    ending = False

    def finish(ok, detail):
        nonlocal ending
        if ending:
            return
        ending = True
        outcome.update(ok=ok, detail=detail, qt_platform=app.platformName(),
                       title=window.windowTitle(), window_visible=window.isVisible())
        report.write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding='utf-8')
        window.close()

    def check():
        if ending or window.store.total < 500:
            return
        try:
            frame = window.store.snapshot()
            assert frame['metadata']['demo'] and frame['metadata']['terminal_configuration']=='DIFFERENTIAL'
            assert np.isfinite(frame['values']).all()
            assert len(frame['values']) >= 500
            window.refresh_plot()
            assert window.readouts['mean'].text() != '—'
            export_snapshot(report.parent / 'проверка_AI3.dat', frame)
            outcome['demo_samples'] = len(frame['values'])
            window.stop()
            finish(True, 'GUI, NI Python API, demo acquisition and UTF-8 export checked; no hardware AI task opened.')
        except Exception:
            finish(False, traceback.format_exc())

    timer = QTimer()
    timer.setInterval(25)
    timer.timeout.connect(check)
    timer.start()
    window.show()
    QTimer.singleShot(0, window.start)
    QTimer.singleShot(20000, lambda: finish(False, 'Windows smoke test timed out.'))
    app.exec_()
    outcome['worker_stopped'] = not window.busy(window.reader)
    outcome['ok'] = bool(outcome['ok'] and outcome['worker_stopped'])
    report.write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if outcome['ok'] else 1


if __name__ == '__main__':
    try:
        if len(sys.argv)==3 and sys.argv[1]=='--self-test':
            sys.exit(smoke_test(sys.argv[2]))
        from app import main
        sys.exit(main())
    except Exception:
        if len(sys.argv)==3 and sys.argv[1]=='--self-test':
            Path(sys.argv[2]).write_text(json.dumps({'ok':False,'error':traceback.format_exc()}),encoding='utf-8')
        raise
