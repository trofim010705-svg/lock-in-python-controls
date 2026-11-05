"""Real worker/Qt event loop, simulated USB, exact files and recovery paths."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from threading import get_ident
import json
import tempfile
import time
import unittest
from unittest.mock import patch
import numpy as np
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication
import gui_app as g
from measurement.ui import MeasurementWindow
from measurement.data import Parameters,save_pair
from measurement.histogram import Histogram


def metadata(path):
    text=Path(path).read_text(encoding='utf-8')
    return json.loads(text.split('CONFIGURATION AND RESULT\n',1)[1].split('\n\nSAMPLE TIMING / STATUS',1)[0])


class FakeUSB(g.DemoTransport):
    def __init__(self,mode=0,current=0,slow=0,fail_after=None,reject_write=False):
        super().__init__();self.data['REFMODE']=str(mode);self.data['IMODE']=str(current)
        self.commands=[];self.slow=slow;self.fail_after=fail_after;self.pairs=0;self.reject_write=reject_write
    def exchange(self,command,*args,**kwargs):
        self.commands.append((command,get_ident(),time.monotonic()))
        if command.startswith('REFP') and ' ' in command and self.reject_write:raise g.CommandError(command,5)
        if command in ('MAG.','MAG1.','PHA.','PHA1.'):
            time.sleep(self.slow)
            if command.startswith('PHA'):
                if self.fail_after is not None and self.pairs>=self.fail_after:raise RuntimeError('USB unplugged')
                self.pairs+=1;return '-31.25',0x29,0
            return str(1e-3+self.pairs*1e-7),0x31,1
        return super().exchange(command)


class FileAndHistogramTests(unittest.TestCase):
    def test_parameters_finite_and_histogram_counts_include_every_value(self):
        p=Parameters(100,-200,.1,20);p.validate()
        for changes in (dict(delay_ms=0),dict(acquisition_time_s=0),dict(sig_mv=float('nan'))):
            with self.assertRaises(ValueError):replace(p,**changes).validate()
        h=Histogram()
        all_values=[]
        for values in ([1.,1.,1.],[0.,2.,1.],[-50.,100.],[.5,.5]):
            all_values+=values;h.add(values);edges,counts=h.update()
            np.testing.assert_array_equal(counts,np.histogram(all_values,bins=edges)[0])
            self.assertEqual(sum(counts),len(all_values))
        old=h.counts.copy();h.update();np.testing.assert_array_equal(old,h.counts)

    def test_unicode_output_unique_pair_and_failure_cleanup(self):
        m=dict(started_utc='2026-11-02T12:00:00.123456+00:00',lockin=dict(magnitude_unit='V'),experiment=dict(additional='Образец α'))
        rows=[(0.,.002,1e-8,-31.25,0x31,1)]
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'Измерения';first=save_pair(folder,m,rows);before=Path(first['data']).read_bytes()
            second=save_pair(folder,m,rows)
            self.assertNotEqual(first,second);self.assertEqual(Path(first['data']).read_bytes(),before)
            self.assertEqual(metadata(first['metadata'])['experiment']['additional'],'Образец α')
            np.testing.assert_allclose(np.loadtxt(first['data'],skiprows=1,ndmin=2),[[1e-8,-31.25]])
            with patch('measurement.data.os.replace',side_effect=PermissionError('disk full')):
                with self.assertRaises(PermissionError):save_pair(folder,m,rows)
            self.assertEqual(len(list(folder.glob('*.dat'))),2)
            self.assertFalse(list(folder.glob('*.tmp')));self.assertFalse(list(folder.glob('*.reserve')))


class MeasurementGUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([]);cls.app.setQuitOnLastWindowClosed(False)
    def wait(self,w,predicate,timeout=8):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            self.app.processEvents()
            if predicate():return
            time.sleep(.002)
        self.fail(f'{w.cycle_state}: {w.cycle_status.text()}\n{w.log.toPlainText()}')
    def cleanup(self,w):
        if w.cycle_state=='unsaved':w.cycle_state='idle' # only tests may explicitly discard their temporary data
        w.close();self.wait(w,lambda:w.thread_stopped)
        w.deleteLater();self.app.processEvents()
    def window(self,transport=None):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        transport=transport or FakeUSB()
        patcher=patch.object(g,'DemoTransport',return_value=transport);patcher.start();self.addCleanup(patcher.stop)
        w=MeasurementWindow(Namespace(demo=True,serial=None,timeout=100,max_frequency=120000));w.show()
        self.addCleanup(self.cleanup,w);w.folder.setText(tmp.name)
        w.submit('open');self.wait(w,lambda:w.connected and not w.busy and not w.pending_jobs and not w.pending_edits)
        return w,transport,Path(tmp.name)
    def run_cycle(self,w,duration=.16,delay=20):
        w.acquisition_time.setValue(duration);w.delay.setValue(delay);w.start_cycle()
        self.wait(w,lambda:w.last_result is not None)
        return w.last_result

    def test_idle_is_live_without_recording_and_complete_cycle_exact_pair_metadata(self):
        w,t,folder=self.window()
        self.wait(w,lambda:w.live_mag.text()!='—')
        self.assertFalse(list(folder.iterdir()));self.assertEqual(w.count,0)
        self.assertEqual(len(w.histograms[0].values),0)
        w.sig.setValue(400);w.bal.setValue(-200);w.e_field.setText('2.1 kV/cm');w.h_field.setText('53.2 mT');w.additional.setPlainText('Образец A\nТемпература 77 K')
        result=self.run_cycle(w)
        self.assertTrue(result['ok'],result)
        r=metadata(result['paths']['metadata']);table=np.loadtxt(result['paths']['data'],skiprows=1,ndmin=2)
        self.assertEqual(table.shape,(r['sample_count'],2));self.assertGreater(len(table),1)
        self.assertTrue(np.all(table[:,1]==-31.25));self.assertEqual(r['result'],'complete')
        self.assertEqual(r['experiment']['sig_mv'],400);self.assertEqual(r['experiment']['bal_mv'],-200)
        self.assertEqual(r['experiment']['h_field'],'53.2 mT');self.assertIn('77 K',r['experiment']['additional'])
        self.assertIn('REFP.',r['lockin']['raw_replies']);self.assertIn('TC',r['lockin']['raw_replies'])
        self.assertEqual(r['sampling']['overload_samples'],len(table))
        self.assertEqual(r['sampling']['reference_unlock_samples'],len(table))
        for histogram in w.histograms:self.assertEqual(histogram.counts.sum(),len(table))
        for histogram in w.histograms:self.assertFalse(histogram.values)
        self.assertTrue(w.tabs.isEnabled());self.assertTrue(w.cycle_start_button.isEnabled())
        worker_ids={item[1] for item in t.commands}
        self.assertEqual(len(worker_ids),1);self.assertNotIn(get_ident(),worker_ids)
        commands=[item[0] for item in t.commands]
        self.assertIn('MAG.',commands);self.assertIn('PHA.',commands)
        self.assertFalse(any(c.startswith(('CBD ','LEN ')) for c in commands))

    def test_stop_saves_partial_and_second_start_resets_only_histogram(self):
        w,t,folder=self.window();w.acquisition_time.setValue(3);w.delay.setValue(10);w.start_cycle()
        self.wait(w,lambda:w.count>=2);w.stop_cycle();self.wait(w,lambda:w.last_result is not None)
        first=w.last_result;self.assertEqual(first['metadata']['result'],'stopped')
        content=Path(first['paths']['data']).read_bytes();old_count=w.count
        second=self.run_cycle(w,.08,20)
        self.assertNotEqual(first['paths'],second['paths']);self.assertEqual(Path(first['paths']['data']).read_bytes(),content)
        self.assertEqual(w.count,second['metadata']['sample_count'])
        for histogram in w.histograms:self.assertEqual(histogram.counts.sum(),w.count)
        self.assertEqual(len(list(folder.glob('*.dat'))),2)

    def test_dual_reference_and_current_units_use_correct_native_commands(self):
        w,t,folder=self.window(FakeUSB(mode=2,current=1))
        result=self.run_cycle(w,.08,20);r=metadata(result['paths']['metadata'])
        self.assertEqual(r['lockin']['measurement_commands'],['MAG1.','PHA1.'])
        self.assertTrue(Path(result['paths']['data']).read_text().startswith('Magn1_A\tPhase1_deg'))
        self.assertEqual(r['lockin']['magnitude_unit'],'A')
        self.assertIn('REFP2.',r['lockin']['raw_replies'])
        self.assertIn('1e-09 A',r['lockin']['sections']['Reference / Output 1']['display_values']['Sensitivity'])

    def test_slow_usb_no_backlog_and_gui_event_loop_stays_responsive(self):
        w,t,folder=self.window(FakeUSB(slow=.02));ticks=[]
        timer=QTimer();timer.setInterval(5);timer.timeout.connect(lambda:ticks.append(1));timer.start()
        result=self.run_cycle(w,.22,1);timer.stop()
        self.assertTrue(result['ok']);self.assertGreater(len(ticks),5)
        self.assertLess(w.count,15);self.assertGreater(w.count,1)
        self.assertGreater(result['metadata']['sampling']['actual_mean_period_ms'],35)

    def test_usb_loss_saves_only_completed_pairs_and_disconnects(self):
        w,t,folder=self.window(FakeUSB(fail_after=2));result=self.run_cycle(w,1,10)
        self.assertTrue(result['ok']);self.assertEqual(result['metadata']['result'],'usb_error')
        self.assertEqual(w.count,2);self.assertFalse(w.connected)
        self.assertIn('USB unplugged',result['metadata']['acquisition_error'])
        self.assertEqual(np.loadtxt(result['paths']['data'],skiprows=1,ndmin=2).shape,(2,2))

    def test_save_failure_retains_data_and_retry_writes_original_parameters(self):
        w,t,folder=self.window();w.additional.setPlainText('Не потерять')
        with patch('measurement.engine.save_pair',side_effect=PermissionError('Access denied')):
            result=self.run_cycle(w,.08,10)
        self.assertFalse(result['ok']);self.assertEqual(w.cycle_state,'unsaved')
        self.assertFalse(w.cycle_start_button.isEnabled());count=w.count
        self.assertEqual(len(w.histograms[0].values),count)
        w.close();self.app.processEvents();self.assertFalse(w.closing)
        w.retry_save();self.wait(w,lambda:w.cycle_state=='idle')
        self.assertEqual(w.count,count);self.assertTrue(w.last_result['ok'])
        self.assertEqual(metadata(w.last_result['paths']['metadata'])['experiment']['additional'],'Не потерять')

    def test_stop_during_preparation_and_close_running_cycle(self):
        w,t,folder=self.window();w.start_cycle();w.stop_cycle()
        self.wait(w,lambda:w.last_result is not None)
        self.assertEqual(w.last_result['metadata']['sample_count'],0)
        self.assertEqual(w.last_result['metadata']['result'],'stopped_before_acquisition')
        w.acquisition_time.setValue(4);w.start_cycle();self.wait(w,lambda:w.count>=2)
        w.close();self.wait(w,lambda:w.thread_stopped)
        self.assertEqual(w.last_result['metadata']['result'],'stopped')
        self.assertTrue(Path(w.last_result['paths']['data']).exists())

    def test_pending_setting_is_written_before_snapshot_and_rejection_cancels_start(self):
        w,t,folder=self.window()
        box=w.sections['Reference / Output 1'][0];box.widgets['refp1'].setValue(27.5)
        result=self.run_cycle(w,.06,20)
        self.assertEqual(float(result['metadata']['lockin']['raw_replies']['REFP.']),27.5)
        t.reject_write=True;box.widgets['refp1'].setValue(35)
        previous=set(folder.iterdir());w.start_cycle()
        self.wait(w,lambda:w.cycle_state=='idle' and not w.busy and not w.pending_edits and not w.edit_timer.isActive())
        self.assertIsNone(w.last_result);self.assertEqual(set(folder.iterdir()),previous)
        self.assertIn('откло',w.cycle_status.text())


if __name__=='__main__':unittest.main()
