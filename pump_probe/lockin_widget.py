"""Embed the existing lock-in UI and grant an exclusive acquisition lease.

The serial worker never touches VISA. All sample requests are queued onto the
same USB worker used by the original gui_app.py, preventing competing reads.
"""
from PyQt5.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import QLabel
from gui_app import LockinWindow


class LockInWidget(LockinWindow):
    experiment_reply = pyqtSignal(str, int, object)
    connection_changed = pyqtSignal(bool)

    def __init__(self, args):
        self.reservation = None
        self.reservation_phase = None
        self.outstanding = None
        self.pending_release = None
        super().__init__(args)
        self.setWindowFlags(Qt.Widget)
        self.lease_banner = QLabel('Локин занят экспериментом. Настройки и фоновый опрос временно приостановлены.')
        self.lease_banner.setWordWrap(True)
        self.lease_banner.setStyleSheet('background: #e7f2ff; color: #164e81; padding: 10px; border-radius: 6px;')
        self.centralWidget().layout().insertWidget(0, self.lease_banner)
        self.lease_banner.hide()

    def update_enabled(self):
        super().update_enabled()
        if self.reservation is not None:
            self.tabs.setEnabled(False)
            for w in (self.connect_button, self.disconnect_button, self.refresh_button, self.legacy, self.auto_poll):
                w.setEnabled(False)
        else:
            self.legacy.setEnabled(not self.closing)
            self.auto_poll.setEnabled(not self.closing)
        if hasattr(self, 'lease_banner'):
            self.lease_banner.setVisible(self.reservation is not None)

    def schedule_poll(self, *args):
        if self.reservation is not None:
            self.poll_timer.stop()
        else:
            super().schedule_poll(*args)

    def submit(self, job, payload=None):
        if self.reservation is not None:
            return False
        return super().submit(job, payload)

    def queue_section(self, box):
        if self.reservation is None:
            super().queue_section(box)

    def dispatch_next(self):
        if self.reservation_phase == 'active':
            return
        super().dispatch_next()
        if (self.reservation_phase == 'draining' and not self.busy and not self.pending_jobs
                and not self.pending_edits and not self.edit_timer.isActive()):
            if not self.connected:
                op, token, payload = self.reservation
                self.reservation_phase = 'active'
                self.experiment_reply.emit(op, token, dict(ok=False, error='Соединение с локином потеряно'))
                return
            op, token, payload = self.reservation
            self.reservation_phase = 'active'
            self.outstanding = (op, token)
            super().start_job('experiment_prepare', payload)

    @pyqtSlot(str, int, object)
    def handle_experiment_request(self, operation, token, payload):
        if operation == 'prepare':
            if self.reservation is not None or not self.connected or self.closing:
                self.experiment_reply.emit(operation, token, dict(ok=False, error='Подключите свободный локин перед запуском'))
                return
            self.reservation = (operation, token, payload)
            self.reservation_phase = 'draining'
            self.poll_timer.stop(); self.update_enabled(); self.dispatch_next()
            return
        if self.reservation is None or payload.get('lease') != self.reservation[1]:
            self.experiment_reply.emit(operation, token, dict(ok=operation == 'release', error='Сеанс локина уже закрыт'))
            return
        if operation == 'release' and self.reservation_phase == 'draining':
            self._release_ui()
            self.experiment_reply.emit(operation, token, dict(ok=True, result={}))
            self.dispatch_next()
            return
        if self.outstanding is not None or self.busy:
            if operation == 'release':
                self.pending_release = (operation, token, payload)
            else:
                self.experiment_reply.emit(operation, token, dict(ok=False, error='Предыдущий запрос локина ещё выполняется'))
            return
        if not self.connected:
            message = 'USB локина закрыт; восстановление CBD/LEN не подтверждено. Проверьте их после подключения.'
            if operation == 'release':
                self._release_ui()
            self.experiment_reply.emit(operation, token, dict(ok=False, error=message))
            return
        self.outstanding = (operation, token)
        super().start_job('experiment_'+operation, payload)

    def _release_ui(self):
        self.reservation = self.reservation_phase = self.outstanding = None
        self.pending_release = None
        self.page_loaded.clear(); self.update_enabled()

    @pyqtSlot(str, object)
    def on_result(self, job, data):
        if not job.startswith('experiment_'):
            was_connected = self.connected
            super().on_result(job, data)
            if was_connected != self.connected:
                self.connection_changed.emit(self.connected)
            return
        operation, token = self.outstanding
        self.outstanding = None; self.busy = False; self.inflight = None
        if operation == 'release':
            self._release_ui()
        self.experiment_reply.emit(operation, token, dict(ok=True, result=data))
        if self.pending_release is not None:
            pending, self.pending_release = self.pending_release, None
            self.handle_experiment_request(*pending)
        elif operation == 'release' and self.connected and not self.closing:
            self.refresh()

    @pyqtSlot(str, str, bool)
    def on_error(self, job, message, fatal):
        was_connected = self.connected
        pending = self.outstanding
        super().on_error(job, message, fatal)
        if was_connected != self.connected:
            self.connection_changed.emit(self.connected)
        if job.startswith('experiment_') and pending is not None:
            operation, token = pending
            self.outstanding = None
            if operation == 'release':
                self._release_ui()
            self.experiment_reply.emit(operation, token, dict(ok=False, error=message))

        if self.pending_release is not None and self.outstanding is None and not self.busy:
            pending, self.pending_release = self.pending_release, None
            self.handle_experiment_request(*pending)
