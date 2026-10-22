import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import numpy as np
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
import gui_app as g
from pump_probe.acquisition import LockInAcquisition, SNAPSHOT_MASK
from pump_probe.data import ScanConfig, ScanWriter
from pump_probe.esp301 import ESP301, DemoESP301, ESPError, ESPConnectionError
from pump_probe.ui import ExperimentWindow


class FakeSerial:
    def __init__(self, **kwargs):
        self.settings, self.commands, self.responses = kwargs, [], []
        self.closed, self.echo, self.motor, self.units = False, False, 0, 2
        self.position = 0.
        self.error_queue = []
        self.timeout_command = None

    def reset_input_buffer(self):
        self.responses.clear()

    def write(self, raw):
        self.commands.append(raw)
        command = raw.decode().rstrip('\r')
        answers = {'VE?': 'ESP301 Version 1.0', '1SN?': str(self.units), '1SL?': '-10',
                   '1SR?': '10', '1ID?': 'DELAY,1234', '1MO?': str(self.motor),
                   '1TP': str(self.position), '1MD?': '1'}
        if command == 'TB?':
            answer = self.error_queue.pop(0) if self.error_queue else '0,0,NO ERROR DETECTED'
        else:
            answer = answers.get(command)
        if command == '1MO': self.motor = 1
        if command.startswith('1PA'): self.position = float(command[3:])
        if command == '1OR': self.position = 0
        if answer is not None and command != self.timeout_command:
            if self.echo: self.responses.append(raw+b'\n')
            self.responses.append((answer+'\r\n').encode())
        return len(raw)

    def read_until(self, expected=b'\n', size=4096):
        return self.responses.pop(0) if self.responses else b''

    def close(self): self.closed = True


class DriverTests(unittest.TestCase):
    def driver(self):
        fake = FakeSerial()
        def factory(**settings): fake.settings = settings; return fake
        driver = ESP301('COM5', serial_factory=factory)
        return driver, fake

    def test_manual_command_framing_units_and_motor_enable(self):
        driver, fake = self.driver(); info = driver.open()
        self.addCleanup(driver.close)
        self.assertEqual(fake.settings['baudrate'], 921600)
        self.assertEqual(fake.settings['bytesize'], 8)
        self.assertEqual(info['limits'], (-10., 10.))
        self.assertFalse(any(x.startswith(b'1MO\r') or x.startswith(b'1OR') for x in fake.commands))
        driver.move_absolute(.125)
        self.assertIn(b'1MO\r', fake.commands)
        self.assertIn(b'1PA0.125000000\r', fake.commands)
        self.assertIn(b'1TP\r', fake.commands); self.assertNotIn(b'1TP?\r', fake.commands)
        self.assertTrue(driver.motion_done()); self.assertEqual(driver.position(), .125)
        driver.home(); driver.stop()
        self.assertIn(b'1OR\r', fake.commands); self.assertIn(b'1ST\r', fake.commands)
        self.assertTrue(all(x.endswith(b'\r') and not x.endswith(b'\r\n') for x in fake.commands))

    def test_travel_limits_and_units_are_not_silently_changed(self):
        driver, fake = self.driver(); driver.open(); self.addCleanup(driver.close)
        with self.assertRaises(ESPError): driver.move_absolute(11)
        self.assertFalse(any(x.startswith(b'1PA') for x in fake.commands))
        driver.close(); fake.units = 3
        with self.assertRaisesRegex(ESPError, 'SN=3'): driver.open()
        self.assertTrue(fake.closed)
        self.assertNotIn(b'1SN2\r', fake.commands)

    def test_timeout_echo_and_controller_error_message(self):
        driver, fake = self.driver(); fake.echo = True; driver.open(); self.addCleanup(driver.close)
        self.assertEqual(driver.position(), 0)
        fake.error_queue.append('107,123,NEGATIVE SOFTWARE LIMIT EXCEEDED')
        with self.assertRaisesRegex(ESPError, '107'): driver.move_absolute(1)
        fake.timeout_command = '1TP'
        with self.assertRaises(ESPConnectionError): driver.position()


class StorageTests(unittest.TestCase):
    def test_endpoints_descending_single_point_and_point_limit(self):
        config = ScanConfig(0, 1, .3, 0, 'unused.dat')
        self.assertEqual(config.positions(), [0, .3, .6, .9, 1])
        self.assertEqual(replace(config, start=1, stop=0).positions(), [1, .7, .4, .1, 0])
        self.assertEqual(replace(config, stop=0).positions(), [0])
        for invalid in (replace(config, step=0), replace(config, step=1e-9), replace(config, delay_ms=-1)):
            with self.assertRaises(ValueError): invalid.positions()

    def test_incremental_rows_comments_and_non_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            config = ScanConfig(0, 1, 1, 0, str(Path(folder)/'scan.dat'), 'Образец A\n300 K')
            metadata = dict(lockin=dict(unit='V'), stage=dict(axis=1))
            writer = ScanWriter(config, metadata); path = Path(writer.open())
            writer.append(.123, [1, 2, 3, 4])
            self.assertEqual(np.loadtxt(path).shape, (5,))  # Visible before close.
            writer.close('stopped', 'Остановлено пользователем')
            contents = path.read_bytes()
            notes = writer.comments_path.read_text()
            self.assertIn('Образец A\n300 K', notes); self.assertIn('RESULT: stopped', notes)
            other = ScanWriter(config, metadata); second = Path(other.open()); other.close('complete')
            self.assertNotEqual(path, second); self.assertEqual(path.read_bytes(), contents)


class AcquisitionTests(unittest.TestCase):
    def setup_session(self):
        transport = g.DemoTransport(); transport.open(); transport.data['REFMODE'] = '2'
        instrument = g.Instrument(transport, g.schema(120000), 120000)
        return LockInAcquisition(instrument), transport

    def test_correlated_four_value_snapshot_and_configuration_restore(self):
        session, transport = self.setup_session()
        metadata = session.prepare()
        self.assertEqual(metadata['acquisition'], 'snapshot')
        self.assertEqual(transport.data['CBD'], str(SNAPSHOT_MASK))
        self.assertEqual(len(session.sample(.5)['values']), 4)
        session.restore()
        self.assertEqual(transport.data['CBD'], '3'); self.assertEqual(transport.data['LEN'], '1000')

    def test_existing_curves_are_preserved_and_pair_fallback_is_explicit(self):
        session, transport = self.setup_session(); transport.data['M'] = '0,1,1,25'
        transport.data['CBD'] = '7'
        metadata = session.prepare()
        self.assertEqual(metadata['acquisition'], 'paired')
        self.assertEqual(transport.data['CBD'], '7')
        self.assertEqual(len(session.sample(.1)['values']), 4)
        session.restore(); self.assertEqual(transport.data['M'], '0,1,1,25')

    def test_unsupported_fast_query_restores_configuration_and_uses_pairs(self):
        session, transport = self.setup_session(); original = transport.exchange
        def exchange(cmd):
            if cmd == '?': raise g.CommandError(cmd, 3)
            return original(cmd)
        transport.exchange = exchange
        metadata = session.prepare()
        self.assertEqual(metadata['acquisition'], 'paired')
        self.assertEqual(transport.data['CBD'], '3'); self.assertEqual(transport.data['LEN'], '1000')
        self.assertEqual(len(session.sample(.2)['values']), 4)

    def test_single_reference_is_rejected_before_snapshot_changes(self):
        session, transport = self.setup_session(); transport.data['REFMODE'] = '0'
        with self.assertRaisesRegex(ValueError, 'Dual reference'): session.prepare()
        self.assertEqual(transport.data['CBD'], '3')


class SlowStage(DemoESP301):
    def move_absolute(self, position):
        super().move_absolute(position)
        self._ends = self._started+max(.08, abs(self._target-self._start)/.5)


class ExperimentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def spin(self, condition, timeout=8):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition(): return
            time.sleep(.002)
        self.fail('Qt timeout')

    def window(self, factory=None):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        args = argparse.Namespace(demo=True, serial=None, timeout=100, max_frequency=120000)
        w = ExperimentWindow(args, factory); w.show()
        def close():
            w.close(); self.spin(lambda:w.stage_stopped and w.lockin.thread_stopped)
        self.addCleanup(close)
        w.outcomes, w.failures, w.paths = [], [], []
        w.worker.finished.connect(lambda *args:w.outcomes.append(args))
        w.worker.error.connect(w.failures.append)
        w.worker.run_started.connect(lambda data:w.paths.append(Path(data['path'])))
        self.spin(lambda:w.pump.stage_connected and w.pump.lockin_connected and not w.lockin.busy)
        w.pump.output.setText(str(Path(folder.name)/'scan.dat')); w.pump.output_is_default = False
        w.pump.stop_position.setValue(.03); w.pump.step.setValue(.01); w.pump.delay.setValue(0)
        return w

    def test_complete_scan_four_plots_files_lease_and_worker_threads(self):
        w = self.window(); transport = w.lockin.worker.instrument.transport
        original = transport.exchange; thread_ids, commands = [], []
        def exchange(cmd):
            thread_ids.append(threading.get_ident()); commands.append(cmd); return original(cmd)
        transport.exchange = exchange
        w.pump.comments.setPlainText('Pump 10 mW; Probe 1 mW; B=20 mT')
        w.pump.start_scan()
        self.spin(lambda:w.worker.state == 'sampling' or bool(w.outcomes))
        if not w.outcomes:
            self.assertIsNotNone(w.lockin.reservation)
            self.assertFalse(w.lockin.tabs.isEnabled())
        self.spin(lambda:bool(w.outcomes))
        self.assertEqual(w.outcomes[-1][0], 'complete'); self.assertFalse(w.failures)
        rows = np.loadtxt(w.paths[-1])
        np.testing.assert_allclose(rows[:, 0], [0, .01, .02, .03], atol=1e-9)
        self.assertEqual(rows.shape, (4, 5)); self.assertEqual(w.pump.count, 4)
        w.pump.redraw()
        for curve in w.pump.curves: self.assertEqual(len(curve.getData()[0]), 4)
        self.assertEqual(commands.count('?'), 5)  # one capability probe and four samples.
        self.assertNotIn(threading.get_ident(), thread_ids)
        self.assertEqual(transport.data['CBD'], '3'); self.assertEqual(transport.data['LEN'], '1000')
        self.assertIsNone(w.lockin.reservation)
        self.assertIn('Pump 10 mW', w.paths[-1].with_suffix('.comments.txt').read_text())
        before = w.paths[-1].read_bytes()
        w.pump.reset_requested.emit(); self.spin(lambda:w.pump.count == 0)
        self.assertEqual(w.paths[-1].read_bytes(), before)

    def test_pause_during_motion_repeats_target_and_preserves_point_order(self):
        w = self.window(SlowStage)
        w.pump.start_position.setValue(.2); w.pump.stop_position.setValue(.22); w.pump.step.setValue(.02)
        w.pump.start_scan(); self.spin(lambda:w.pump.state == 'moving')
        w.pump.pause_requested.emit(); self.spin(lambda:w.pump.state == 'paused')
        count = w.pump.count; QTest.qWait(100); self.assertEqual(w.pump.count, count)
        w.pump.pause_requested.emit(); self.spin(lambda:bool(w.outcomes))
        rows = np.atleast_2d(np.loadtxt(w.paths[-1]))
        np.testing.assert_allclose(rows[:, 0], [.2, .22], atol=1e-9)
        self.assertEqual(w.outcomes[-1][0], 'complete')
        self.assertTrue(any(cmd[0] == 'ST' for cmd in w.worker.driver.commands))

    def test_pause_during_settling_does_not_acquire_until_resume(self):
        w = self.window(); w.pump.stop_position.setValue(0); w.pump.delay.setValue(150)
        w.pump.start_scan(); self.spin(lambda:w.pump.state == 'settling')
        w.pump.pause_requested.emit(); self.spin(lambda:w.pump.state == 'paused')
        QTest.qWait(200); self.assertEqual(w.pump.count, 0)
        w.pump.pause_requested.emit(); self.spin(lambda:bool(w.outcomes))
        self.assertEqual(w.pump.count, 1)

    def test_stop_retains_partial_file_and_home_uses_existing_stage_mode(self):
        w = self.window(); w.pump.stop_position.setValue(1); w.pump.delay.setValue(20)
        w.pump.start_scan(); self.spin(lambda:w.pump.count >= 2)
        w.pump.stop_requested.emit(); self.spin(lambda:bool(w.outcomes))
        self.assertEqual(w.outcomes[-1][0], 'stopped')
        rows = np.atleast_2d(np.loadtxt(w.paths[-1]))
        self.assertTrue(2 <= len(rows) < 101); self.assertEqual(len(rows), w.pump.count)
        self.assertIn('RESULT: stopped', w.paths[-1].with_suffix('.comments.txt').read_text())
        driver = w.worker.driver
        states = []
        w.worker.state_changed.connect(lambda state,text:states.append(state))
        w.pump.home_requested.emit(5.)
        self.spin(lambda:'homing' in states and states[-1] == 'idle')
        self.assertAlmostEqual(driver.position(), 0, places=6)

    def test_disk_path_failure_occurs_before_motion_and_releases_lockin(self):
        w = self.window()
        blocker = Path(w.pump.output.text()).parent/'not_a_directory'; blocker.write_text('keep')
        w.pump.output.setText(str(blocker/'scan.dat'))
        w.pump.start_scan(); self.spin(lambda:bool(w.outcomes))
        self.assertEqual(w.outcomes[-1][0], 'error')
        self.assertFalse(any(cmd[0] == 'PA' for cmd in w.worker.driver.commands))
        self.assertIsNone(w.lockin.reservation)
        self.assertEqual(blocker.read_text(), 'keep')

    def test_overload_aborts_without_persisting_bad_sample(self):
        w = self.window(); transport = w.lockin.worker.instrument.transport
        original = transport.exchange; count = [0]
        def overload(cmd):
            text, status, ov = original(cmd)
            if cmd == '?':
                count[0] += 1
                if count[0] > 1: status, ov = 0x31, 0x0c
            return text, status, ov
        transport.exchange = overload
        w.pump.start_scan(); self.spin(lambda:bool(w.outcomes))
        self.assertEqual(w.outcomes[-1][0], 'error'); self.assertEqual(w.pump.count, 0)
        self.assertIsNone(w.lockin.reservation)
        self.assertIn('OV=0x0C', w.paths[-1].with_suffix('.comments.txt').read_text())

    def test_usb_loss_stops_stage_keeps_file_and_unblocks_reservation(self):
        w = self.window(); transport = w.lockin.worker.instrument.transport
        original = transport.exchange; count = [0]
        def unplug(cmd):
            if cmd == '?':
                count[0] += 1
                if count[0] > 2: raise TimeoutError('USB disconnected')
            return original(cmd)
        transport.exchange = unplug
        w.pump.start_scan(); self.spin(lambda:bool(w.outcomes))
        self.assertEqual(w.outcomes[-1][0], 'error')
        self.assertFalse(w.lockin.connected); self.assertIsNone(w.lockin.reservation)
        self.assertEqual(len(np.atleast_2d(np.loadtxt(w.paths[-1]))), 1)
        self.assertTrue(w.worker.driver.motion_done())
        self.assertIn('RESULT: error', w.paths[-1].with_suffix('.comments.txt').read_text())

    def test_stop_during_usb_read_discards_late_sample_and_keeps_ui_responsive(self):
        w = self.window(); transport = w.lockin.worker.instrument.transport
        original = transport.exchange; count = [0]; returned = []
        def slow(cmd):
            if cmd == '?':
                count[0] += 1
                if count[0] > 1:
                    time.sleep(.3)
                    returned.append(True)
            return original(cmd)
        transport.exchange = slow
        w.pump.start_scan(); self.spin(lambda:w.pump.state == 'sampling')
        w.pump.stop_requested.emit()
        self.spin(lambda:any(cmd[0] == 'ST' for cmd in w.worker.driver.commands))
        self.assertFalse(returned)
        QTest.qWait(50); self.assertEqual(w.pump.count, 0)
        self.spin(lambda:bool(w.outcomes))
        self.assertEqual(w.outcomes[-1][0], 'stopped'); self.assertEqual(w.pump.count, 0)
        self.assertIsNone(w.lockin.reservation)
        self.assertEqual(transport.data['CBD'], '3')

    def test_stop_during_prepare_does_not_start_motion_or_create_data_file(self):
        w = self.window()
        box = w.lockin.sections['Reference / Output 1'][0]
        box.widgets['refp1'].setValue(12)
        w.pump.start_scan(); self.spin(lambda:w.pump.state == 'preparing')
        w.pump.stop_requested.emit(); self.spin(lambda:bool(w.outcomes))
        self.assertEqual(w.outcomes[-1][0], 'stopped')
        self.assertFalse(w.paths)
        self.assertFalse(any(cmd[0] == 'PA' for cmd in w.worker.driver.commands))
        self.assertIsNone(w.lockin.reservation)

    def test_stage_disconnect_stops_scan_and_preserves_previous_rows(self):
        w = self.window(); w.pump.stop_position.setValue(.1); w.pump.delay.setValue(30)
        w.pump.start_scan(); self.spin(lambda:w.pump.count >= 1)
        def lost(): raise ESPConnectionError('COM cable disconnected')
        w.worker.driver.motion_done = lost
        w.worker.driver.stop = lost
        self.spin(lambda:bool(w.outcomes))
        self.assertEqual(w.outcomes[-1][0], 'error')
        self.assertFalse(w.pump.stage_connected)
        self.assertIsNone(w.lockin.reservation)
        self.assertGreaterEqual(len(np.atleast_2d(np.loadtxt(w.paths[-1]))), 1)
        self.assertIn('Остановка оси не подтверждена', w.outcomes[-1][1])

    def test_reset_running_scan_preserves_file_and_clears_only_plots(self):
        w = self.window(); w.pump.stop_position.setValue(.2); w.pump.delay.setValue(20)
        w.pump.start_scan(); self.spin(lambda:w.pump.count >= 1)
        w.pump.reset_requested.emit(); self.spin(lambda:bool(w.outcomes))
        self.assertEqual(w.outcomes[-1][0], 'reset'); self.assertEqual(w.pump.count, 0)
        self.assertGreaterEqual(len(np.atleast_2d(np.loadtxt(w.paths[-1]))), 1)
        self.assertIn('RESULT: reset', w.paths[-1].with_suffix('.comments.txt').read_text())

    def test_stop_during_disk_flush_prevents_next_motion(self):
        w = self.window(); w.pump.delay.setValue(150)
        w.pump.start_scan(); self.spin(lambda:w.pump.state == 'settling')
        writer = w.worker.writer; original = writer.append
        writing = threading.Event()
        def slow(position, values):
            original(position, values); writing.set(); time.sleep(.2)
        writer.append = slow
        self.spin(writing.is_set)
        w.pump.stop_requested.emit()
        self.spin(lambda:bool(w.outcomes))
        self.assertEqual(w.outcomes[-1][0], 'stopped')
        self.assertEqual(sum(cmd[0] == 'PA' for cmd in w.worker.driver.commands), 1)
        self.assertEqual(w.pump.count, 1)

    def test_release_waits_for_inflight_preparation_and_restores_once(self):
        w = self.window(); replies = []
        w.lockin.experiment_reply.connect(lambda *args:replies.append(args))
        transport = w.lockin.worker.instrument.transport; original = transport.exchange
        def slow(cmd): time.sleep(.01); return original(cmd)
        transport.exchange = slow
        w.lockin.handle_experiment_request('prepare', 100, {'lease':100})
        self.spin(lambda:w.lockin.outstanding == ('prepare',100))
        w.lockin.handle_experiment_request('release', 101, {'lease':100})
        self.spin(lambda:any(r[0]=='release' for r in replies))
        self.assertEqual([(r[0],r[1]) for r in replies], [('prepare',100),('release',101)])
        self.assertTrue(replies[-1][2]['ok']); self.assertIsNone(w.lockin.reservation)
        self.assertEqual(transport.data['CBD'], '3')

    def test_closing_during_settling_preserves_file_and_shuts_both_threads(self):
        w = self.window(); w.pump.delay.setValue(200)
        w.pump.start_scan(); self.spin(lambda:w.pump.count >= 1 and w.pump.state == 'settling')
        path = w.paths[-1]
        w.close(); self.spin(lambda:w.stage_stopped and w.lockin.thread_stopped)
        self.assertFalse(w.stage_thread.isRunning()); self.assertFalse(w.lockin.thread.isRunning())
        self.assertGreaterEqual(len(np.atleast_2d(np.loadtxt(path))), 1)
        self.assertIn('RESULT: closed', path.with_suffix('.comments.txt').read_text())


if __name__ == '__main__': unittest.main(verbosity=2)
