import argparse
import os
import sys
import threading
import time
import types
import unittest
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gui_app as g


class RecordingDemo(g.DemoTransport):
    def __init__(self):
        super().__init__(); self.commands=[]
    def exchange(self, command):
        self.commands.append(command)
        return super().exchange(command)


class Commands(unittest.TestCase):
    def setUp(self):
        self.t=RecordingDemo(); self.t.open()
        self.i=g.Instrument(self.t,g.schema(120000),120000)
    def apply(self,title,changes):
        state=self.i.read_section(title)['values']; state.update(changes)
        self.t.commands.clear()
        self.i.apply(title,state,set(changes))
        return self.t.commands
    def test_binary_status(self):
        self.assertEqual(g.parse_reply(b'+1.2E-3\n\0\xC9\xFF'),('+1.2E-3',201,255))
        self.assertEqual(g.parse_reply(b'\0\x01\0'),('',1,0))
        self.assertEqual(g.parse_reply(b'1\0',False),('1',0,0))
        for reply in (b'',b'1',b'1\0',b'1\0\x01'):
            with self.assertRaises(g.ProtocolError): g.parse_reply(reply)
    def test_read_all_modes(self):
        for mode in (0,1,2):
            self.t.data['REFMODE']=str(mode)
            for title in self.i.sections:
                state=self.i.read_section(title)
                self.assertFalse(state['warnings'],(mode,title,state['warnings']))
            self.assertEqual(len(self.i.poll([(0,True),(3,True),(6,False),(9,True)],True,True)['readings']),4)
    def test_channel_commands(self):
        for mode in (0,1,2):
            self.t.data['REFMODE']=str(mode)
            commands=self.apply('Reference / Output 1',{'refp1':12.5,'xof1en':1,'xof1val':-3.25})
            suffix='1' if mode else ''
            self.assertIn(f'REFP{suffix}. 12.500',commands)
            self.assertIn(f'XOF{suffix} 1 -325',commands)
            self.assertNotIn('TC'+suffix+' 9',commands)
    def test_sensitivity_and_constraints(self):
        self.assertIn('SEN 20',self.apply('Reference / Output 1',{'sen1':20}))
        with self.assertRaises(ValueError): self.apply('Reference / Output 1',{'tc1':0})
        self.t.data['NOISEMODE']='1'; self.t.data['FASTMODE']='1'
        with self.assertRaises(ValueError): self.apply('Reference / Output 1',{'slope1':3})
    def test_modulation_order(self):
        commands=self.apply('Frequency modulation (VCO)',{'fmcenterf':2000,'fmspanf':100})
        self.assertEqual(commands[-3:],['FMSPANF. 0','FMCENTERF. 2000.000','FMSPANF. 100.000'])
        with self.assertRaises(ValueError): self.apply('Frequency modulation (VCO)',{'fmspanf':10000})
    def test_sweep_scale(self):
        commands=self.apply('Frequency / Amplitude Sweep',{'fstep':2.5,'fkind':0,'srate':.001})
        self.assertIn('FSTEP. 2.500 0',commands)
        self.assertIn('SRATE. 0.001',commands)
        with self.assertRaises(ValueError): self.apply('Frequency / Amplitude Sweep',{'fstep':101,'fkind':0})
    def test_equation(self):
        title='Equation 1: ((A ± B) × C) / D'
        commands=self.apply(title,{'eq1_0':0,'eq1_1':1,'eq1_2':1,'eq1_3':8,'eq1_4':11,'c11':2})
        self.assertIn('DEFEQU 1 0 1 1 8 11',commands)
        self.assertIn('C1 1 2',commands)
        with self.assertRaises(ValueError): self.apply(title,{'eq1_0':14})
    def test_unchanged_unknown_code_does_not_block_other_edits(self):
        self.t.data['LF']='0,1'  # Off, retained 50 Hz family.
        self.assertIn('ACGAIN 2',self.apply('Input',{'acgain':2}))

    def test_partial_update_stops_after_rejection(self):
        original=self.t.exchange
        def reject(cmd):
            if cmd.startswith('FMCENTERF. '):
                raise g.CommandError(cmd,5)
            return original(cmd)
        self.t.exchange=reject
        with self.assertRaisesRegex(ValueError,'Уже выполнено'):
            self.apply('Frequency modulation (VCO)',{'fmcenterf':2000,'fmspanf':100})
        self.assertEqual(self.t.data['FMSPANF.'],'0')
        self.assertEqual(self.t.data['FMCENTERF.'],'1000')
        self.assertTrue(self.t.opened)

    def test_rs_parity(self):
        self.assertIn('RS 12 3',self.apply('RS232',{'parity':1}))
        self.assertIn('RS 12 7',self.apply('RS232',{'parity':2}))
    def test_dac_and_input(self):
        self.assertIn('DAC. 1 -2.500',self.apply('DACs',{'dac1':-2.5}))
        self.assertIn('CH 1 0',self.apply('DACs',{'ch1':0}))
        with self.assertRaises(ValueError): self.apply('DACs',{'dac1':1})
        self.assertIn('IMODE 2',self.apply('Input',{'input':5}))
    def test_serial_transport(self):
        commands=[]
        class FakeResource:
            def write_raw(self,b): commands.append(b)
            def read_raw(self): return b'1\0\x81\xFF'
        resource=FakeResource()
        fake=types.SimpleNamespace(Connection_Open_USB=lambda:resource, Connection_Close=lambda r:commands.append('close'))
        previous=sys.modules.get('lockin_7230_usb'); sys.modules['lockin_7230_usb']=fake
        try:
            transport=g.USB7230('XYZ',500); transport.open()
            self.assertEqual(transport.exchange('VER'),('1',129,255))
            self.assertTrue(all(c.endswith(b'\0') for c in commands))
            self.assertEqual(resource.read_termination,None)
            transport.close(); self.assertEqual(commands[-1],'close')
            self.assertEqual(fake._USB_RESOURCE,'USB0::0x0A2D::0x0027::XYZ::RAW')
        finally:
            if previous is None: sys.modules.pop('lockin_7230_usb',None)
            else: sys.modules['lockin_7230_usb']=previous


class GUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=g.QApplication.instance() or g.QApplication([])
    def spin(self,predicate,timeout=4):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.app.processEvents()
            if predicate(): return
            time.sleep(.002)
        self.fail('Timeout in Qt event loop')
    def test_tabs_updates_thread_and_shutdown(self):
        args=argparse.Namespace(demo=True,serial=None,timeout=100,max_frequency=120000)
        w=g.LockinWindow(args); w.auto_poll.setChecked(False); w.show()
        failures=[]; w.worker.failed.connect(lambda *a:failures.append(a))
        try:
            w.submit('open'); self.spin(lambda:w.connected and not w.busy)
            self.assertEqual(w.sections['Reference / Output 1'][0].widgets['sen1'].currentText(),'1 mV')
            for page in (1,2,3,0):
                w.tabs.setCurrentIndex(page)
                self.spin(lambda:not w.busy)
                self.assertIn(page,w.page_loaded)
            # Apply a real changed control through queued signals.
            box=w.sections['Reference / Output 1'][0]
            box.widgets['refp1'].setValue(25.5)
            w.apply_section(box.title); self.spin(lambda:not w.busy)
            self.assertEqual(w.worker.instrument.transport.data['REFP.'],'25.500')
            # Every hardware operation executes on a thread other than the GUI thread.
            transport=w.worker.instrument.transport
            original=transport.exchange; tids=[]
            def recording(cmd):
                tids.append(threading.get_ident()); return original(cmd)
            transport.exchange=recording
            w.poll(); self.spin(lambda:not w.busy)
            self.assertTrue(tids); self.assertNotIn(threading.get_ident(),tids)
            # Per-bit editor produces the same mask as the numeric control.
            w.tabs.setCurrentIndex(2); self.spin(lambda:not w.busy)
            w.bit_controls[7][0].click()
            self.assertEqual(w.sections['Digital Port'][0].widgets['portdir'].value(),128)
            self.assertFalse(failures,failures)
            # Simulate loss during polling, then prove a fresh connection works.
            transport.exchange=lambda cmd: (_ for _ in ()).throw(TimeoutError('USB unplugged'))
            w.poll(); self.spin(lambda:not w.busy)
            self.assertFalse(w.connected)
            w.submit('open'); self.spin(lambda:w.connected and not w.busy)
            self.assertEqual(len(failures),1)
        finally:
            w.close(); self.spin(lambda:w.thread_stopped)
            self.assertFalse(w.thread.isRunning())

    def test_close_during_usb_job_keeps_event_loop_alive(self):
        args=argparse.Namespace(demo=True,serial=None,timeout=100,max_frequency=120000)
        w=g.LockinWindow(args); w.auto_poll.setChecked(False); w.show()
        w.submit('open'); self.spin(lambda:w.connected and not w.busy)
        transport=w.worker.instrument.transport
        original=transport.exchange
        def delayed(cmd):
            time.sleep(.025); return original(cmd)
        transport.exchange=delayed
        pulses=[]
        timer=g.QTimer(); timer.setInterval(5); timer.timeout.connect(lambda:pulses.append(1)); timer.start()
        w.poll(); w.close()
        self.spin(lambda:w.thread_stopped)
        timer.stop()
        self.assertGreater(len(pulses),5)
        self.assertFalse(w.thread.isRunning())

if __name__=='__main__': unittest.main(verbosity=2)
