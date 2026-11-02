"""One USB-thread acquisition session, using a monotonic non-overlapping timer."""
from dataclasses import asdict
import math
import time
from PyQt5.QtCore import QObject,QTimer,Qt
from .data import utc_now,save_pair


class PreparationStopped(Exception):
    pass


class SnapshotTransport:
    """Read-only snapshot audit; cancellation is checked before every USB command."""
    def __init__(self,transport,event,answers):self.transport,self.event,self.answers=transport,event,answers
    def exchange(self,command,*args,**kwargs):
        if self.event.is_set():raise PreparationStopped()
        answer=self.transport.exchange(command,*args,**kwargs)
        self.answers[command]=answer[0]
        return answer


class CycleEngine(QObject):
    MAX_SAMPLES=1_000_000

    def __init__(self,worker,parameters,folder,event):
        super().__init__(worker)
        self.worker,self.inst,self.parameters,self.folder,self.event=worker,worker.instrument,parameters,folder,event
        self.rows=[];self.batch=[];self.active=True;self.unsaved=False
        self.started=None;self.last_emit=0.;self.next_sample=None
        self.metadata=dict(application='Lock-in Histogram 1.0',started_utc=utc_now(),
            experiment=asdict(parameters),simulation=bool(worker.args[0]),lockin={},
            sampling=dict(requested_period_ms=parameters.delay_ms,
                pair_acquisition='Sequential MAG[1]. then PHA[1].; not a simultaneous hardware snapshot',
                scheduling='Start-to-start monotonic period; missed periods skipped, never queued.',
                magnitude_conversion='None; physical V/A as returned by the instrument. Sig/Bal are metadata only.',
                phase_conversion='None; native instrument degrees, no unwrap or modulo conversion.'))
        self.metadata['requested_utc']=self.metadata['started_utc']
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)

    def prepare(self):
        real=self.inst.transport
        lockin=self.metadata['lockin'];lockin['raw_replies']={}
        self.inst.transport=SnapshotTransport(real,self.event,lockin['raw_replies'])
        try:
            lockin['firmware']=self.inst.query('VER')
            lockin['usb_resource']=getattr(getattr(real,'module',None),'_USB_RESOURCE','DEMO' if self.worker.args[0] else '')
            self.inst.mode=int(self.inst.values('REFMODE')[0])
            lockin['reference_mode_code']=self.inst.mode
            lockin['input_mode_code']=int(self.inst.values('IMODE')[0])
            lockin['magnitude_unit']='V' if lockin['input_mode_code']==0 else 'A'
            self.mag_command=self.inst.channel_command('MAG',1)+'.'
            self.phase_command=self.inst.channel_command('PHA',1)+'.'
            lockin['measurement_commands']=[self.mag_command,self.phase_command]
            lockin['sections']={}
            for title in self.inst.sections:
                self.worker.progress.emit('Снимок настроек: '+title)
                state=self.inst.read_section(title)
                labels={f.key:f.label for f in self.inst.sections[title]}
                display={}
                for f in self.inst.sections[title]:
                    if f.key not in state['values']:continue
                    value=state['values'][f.key]
                    display[f.label]=next((label for label,code in f.choices if value==code),value)
                    if f.key in ('sen1','sen2'):
                        code=int(value);imode=lockin['input_mode_code']
                        sensitivity=(1,2,5)[code%3]*10**((code//3)-1)*1e-8
                        sensitivity*=1 if imode==0 else 1e-6 if imode==1 else 1e-8
                        display[f.label]=f'{sensitivity:.12g} '+lockin['magnitude_unit']
                lockin['sections'][title]=dict(state,field_labels=labels,display_values=display)
        finally:self.inst.transport=real
        if self.event.is_set():raise PreparationStopped()
        self.started=time.monotonic();self.next_sample=self.started
        self.metadata['started_utc']=utc_now()
        self.metadata['acquisition_started_utc']=self.metadata['started_utc']
        self.worker.cycle_started.emit(self.metadata)
        self.timer.start(0)

    def tick(self):
        if not self.active:return
        now=time.monotonic()
        if self.event.is_set():self.finish('stopped');return
        if now>=self.started+self.parameters.acquisition_time_s:self.finish('complete');return
        if now<self.next_sample:
            self.arm();return
        try:
            t0=time.monotonic()
            mag=self.inst.values(self.mag_command)
            status,overload=self.inst.status,self.inst.overload
            phase=self.inst.values(self.phase_command)
            status|=self.inst.status;overload|=self.inst.overload
            if len(mag)!=1 or len(phase)!=1 or not all(math.isfinite(x) for x in (mag[0],phase[0])):
                raise RuntimeError('Некорректная пара Magn1/Phase1 от прибора.')
            row=(t0-self.started,time.monotonic()-self.started,mag[0],phase[0],status,overload)
            self.rows.append(row);self.batch.append(row)
        except Exception as exc:
            self.metadata['acquisition_error']=f'{type(exc).__name__}: {exc}'
            self.worker.cycle_disconnect(self.metadata['acquisition_error'])
            self.finish('usb_error');return
        if time.monotonic()-self.last_emit>=.1:
            self.flush()
        if len(self.rows)>=self.MAX_SAMPLES:self.finish('memory_limit');return
        period=self.parameters.delay_ms/1000.
        now=time.monotonic()
        self.next_sample+=period
        if self.next_sample<now:
            self.next_sample+=math.ceil((now-self.next_sample)/period)*period
        self.arm()

    def arm(self):
        remaining=min(self.next_sample,self.started+self.parameters.acquisition_time_s)-time.monotonic()
        self.timer.start(max(0,int(math.ceil(remaining*1000))))

    def flush(self):
        if self.batch:
            self.worker.cycle_samples.emit(tuple(self.batch));self.batch=[]
        self.last_emit=time.monotonic()

    def finish(self,reason,error=''):
        if not self.active:return
        self.active=False;self.timer.stop();self.flush()
        if error:self.metadata['acquisition_error']=error
        elapsed=time.monotonic()-self.started if self.started is not None else 0.
        self.metadata.update(finished_utc=utc_now(),result=reason,sample_count=len(self.rows),actual_elapsed_s=elapsed)
        self.metadata['sampling']['overload_samples']=sum(bool(r[5]) for r in self.rows)
        self.metadata['sampling']['reference_unlock_samples']=sum(bool(r[4]&8) for r in self.rows)
        if len(self.rows)>1:
            periods=[b[0]-a[0] for a,b in zip(self.rows,self.rows[1:])]
            self.metadata['sampling'].update(actual_mean_period_ms=sum(periods)/len(periods)*1000,
                actual_min_period_ms=min(periods)*1000,actual_max_period_ms=max(periods)*1000)
        self.save()

    def save(self,folder=None):
        if folder is not None:self.folder=folder
        self.worker.cycle_saving.emit()
        self.worker.progress.emit('Сохранение Magn1 / Phase1…')
        try:
            paths=save_pair(self.folder,self.metadata,self.rows)
            self.unsaved=False
            self.rows.clear()
            self.worker.cycle_finished.emit(dict(ok=True,paths=paths,metadata=self.metadata))
        except Exception as exc:
            self.unsaved=True
            self.worker.cycle_finished.emit(dict(ok=False,error=str(exc),metadata=self.metadata))
