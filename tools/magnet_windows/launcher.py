"""Frozen entry point; normal operation delegates to the unmodified app.py."""
import json
import platform
from pathlib import Path
import sys
import traceback


def smoke_test(report_path):
    from PyQt5.QtCore import Qt, QTimer, QT_VERSION_STR, PYQT_VERSION_STR
    from PyQt5.QtWidgets import QApplication
    from gui import MainWindow

    report = Path(report_path)
    report.parent.mkdir(parents=True, exist_ok=True)
    outcome = {'ok': False, 'frozen': bool(getattr(sys, 'frozen', False)),
               'platform': platform.platform(), 'python': platform.python_version(),
               'qt': QT_VERSION_STR, 'pyqt': PYQT_VERSION_STR}
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv[:1])
    app.setApplicationName('MagnetCommandTester-BuildCheck')
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
        window.close()  # Uses the application's normal asynchronous worker shutdown.

    def completed(operation, success, message, state):
        if ending:
            return
        if not success:
            finish(False, message)
        elif operation == 'open':
            QTimer.singleShot(0, lambda: window.submit('probe'))
        elif operation == 'probe':
            good = (state['identified'] and state['connected']
                    and state['values'].get('OUT?') is False
                    and state['values'].get('MC?') == 0
                    and not state['relay_available'])
            finish(bool(good), 'GUI and demo protocol check; no hardware ports opened.')

    window.worker.completed.connect(completed)
    window.show()
    QTimer.singleShot(0, window.toggle_connection)
    QTimer.singleShot(20000, lambda: finish(False, 'Build smoke test timed out.'))
    app.exec_()
    outcome['worker_stopped'] = not window.thread.isRunning()
    outcome['ok'] = bool(outcome['ok'] and outcome['worker_stopped'])
    report.write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if outcome['ok'] else 1


if __name__ == '__main__':
    try:
        if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
            sys.exit(smoke_test(sys.argv[2]))
        from app import main
        sys.exit(main())
    except Exception:
        if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
            Path(sys.argv[2]).write_text(json.dumps({'ok': False, 'error': traceback.format_exc()}), encoding='utf-8')
        raise
