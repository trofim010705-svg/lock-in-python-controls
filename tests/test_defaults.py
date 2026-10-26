"""Persistent numeric defaults without connecting either instrument."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
import tempfile
import unittest
from PyQt5.QtCore import QPoint, QSettings, QTimer
from PyQt5.QtWidgets import QApplication
from pump_probe.ui import PumpProbeWidget


class DefaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def widget(self, path):
        w = PumpProbeWidget(settings=QSettings(str(path), QSettings.IniFormat))
        self.addCleanup(w.deleteLater)
        return w

    def test_only_explicitly_saved_fields_survive_new_instance(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'defaults.ini'
            w = self.widget(path)
            w.delay.setValue(250)
            w.step.setValue(.125)
            w.save_default('step', w.step)
            w.axis.setValue(2)
            w.save_default('axis', w.axis)
            other = self.widget(path)
            self.assertEqual(other.step.value(), .125)
            self.assertEqual(other.axis.value(), 2)
            self.assertEqual(other.delay.value(), 100)
            self.assertEqual(other.tolerance.value(), .020)

    def test_menu_saves_uncommitted_text_and_keeps_standard_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'defaults.ini'
            w = self.widget(path)
            w.start_position.lineEdit().setText('12.345')
            actions = []
            def choose():
                menu = self.app.activePopupWidget()
                actions.extend(a.text() for a in menu.actions())
                menu.actions()[-1].trigger()
                menu.close()
            QTimer.singleShot(0, choose)
            w.start_position.lineEdit().customContextMenuRequested.emit(QPoint(2, 2))
            self.assertGreater(len(actions), 2)
            self.assertEqual(actions[-1], 'Сделать значением по умолчанию')
            self.assertEqual(self.widget(path).start_position.value(), 12.345)

    def test_corrupt_or_out_of_range_defaults_and_disabled_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'defaults.ini'
            settings = QSettings(str(path), QSettings.IniFormat)
            for key, value in dict(step='NaN', axis=5, tolerance='broken', delay=-1).items():
                settings.setValue('delay_line_defaults/'+key, value)
            settings.sync()
            w = self.widget(path)
            self.assertEqual(w.step.value(), .5)
            self.assertEqual(w.axis.value(), 1)
            self.assertEqual(w.tolerance.value(), .020)
            self.assertEqual(w.delay.value(), 100)
            w.delay.setValue(600)
            w.delay.setEnabled(False)
            w.save_default('delay', w.delay)
            self.assertEqual(self.widget(path).delay.value(), 100)


if __name__ == '__main__':
    unittest.main()
