"""Extend the existing single USB worker; no competing VISA connections."""
from PyQt5.QtCore import pyqtSignal,pyqtSlot
from gui_app import Worker
from .engine import CycleEngine,PreparationStopped


class MeasurementWorker(Worker):
    cycle_started=pyqtSignal(object)
    cycle_samples=pyqtSignal(object)
    cycle_finished=pyqtSignal(object)
    cycle_saving=pyqtSignal()

    def __init__(self,*args):
        super().__init__(*args);self.session=None

    @pyqtSlot(str,object)
    def execute(self,job,payload):
        if job=='cycle_stop':
            if self.session and self.session.active:self.session.finish('stopped')
            return
        if job=='cycle_save':
            if self.session and self.session.unsaved:self.session.save(payload['folder'])
            return
        if job=='cycle_start':
            if self.instrument is None:
                self.failed.emit(job,'Нет соединения с локином',False);return
            if self.session and (self.session.active or self.session.unsaved):
                self.failed.emit(job,'Предыдущий цикл ещё не завершён/не сохранён',False);return
            if self.session:self.session.deleteLater()
            self.instrument.legacy=payload.get('legacy',False)
            self.session=CycleEngine(self,payload['parameters'],payload['folder'],payload['event'])
            try:
                self.session.parameters.validate();self.session.prepare()
            except PreparationStopped:self.session.finish('stopped_before_acquisition')
            except Exception as exc:
                self.cycle_disconnect(str(exc));self.session.finish('preparation_error',str(exc))
            self.result.emit(job,{})
            return
        if self.session and self.session.active:
            self.failed.emit(job,'Прибор занят измерительным циклом.',False);return
        super().execute(job,payload)

    def cycle_disconnect(self,message):
        if self.instrument:
            try:self.instrument.transport.close()
            except Exception:pass
        self.instrument=None
        self.failed.emit('cycle_usb',message,True)
