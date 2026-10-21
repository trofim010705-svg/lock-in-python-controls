"""Non-blocking scan state machine; this object owns serial and disk I/O.

Qt timers wait for motion and settling without sleeps. VISA acquisition is an
asynchronous request to LockInWidget's existing, exclusive USB worker.
"""
import time
from threading import Event
from PyQt5.QtCore import QObject, QTimer, Qt, pyqtSignal, pyqtSlot
from .esp301 import ESP301, DemoESP301, ESPConnectionError, ESPError
from .data import ScanWriter


class ExperimentWorker(QObject):
    state_changed = pyqtSignal(str, str)
    stage_changed = pyqtSignal(bool, object)
    position_changed = pyqtSignal(float)
    run_started = pyqtSignal(object)
    point_ready = pyqtSignal(int, object)
    finished = pyqtSignal(str, str)
    error = pyqtSignal(str)
    lockin_request = pyqtSignal(str, int, object)
    stopped = pyqtSignal()

    def __init__(self, demo=False, driver_factory=None):
        super().__init__()
        self.driver_factory = driver_factory or (DemoESP301 if demo else ESP301)
        self.driver = self.info = self.config = self.writer = None
        self.timer = None
        self.state = 'idle'
        self.cancel_event = Event()
        self.sequence = self.lease = 0
        self.awaiting = None
        self.stop_requested = self.pause_requested = self.shutting_down = False
        self.stage_stopped = True
        self.outcome, self.message = 'complete', ''
        self.targets, self.index, self.actual = [], 0, 0.
        self.deadline = self.last_position_time = 0.

    def set_state(self, state, message):
        self.state = state
        self.state_changed.emit(state, message)

    @pyqtSlot()
    def initialize(self):
        self.timer = QTimer(self)
        self.timer.setInterval(10); self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    @pyqtSlot(object)
    def connect_stage(self, settings):
        if self.state != 'idle' or self.driver is not None:
            return
        try:
            self.driver = self.driver_factory(**settings)
            self.info = self.driver.open()
            self.actual = self.info['position']
            self.stage_changed.emit(True, self.info)
            self.position_changed.emit(self.actual)
            self.set_state('idle', 'Линия задержки подключена')
        except Exception as exc:
            if self.driver:
                try:
                    self.driver.close()
                except Exception:
                    pass
            self.driver = None
            self.stage_changed.emit(False, {})
            self.error.emit(str(exc))

    @pyqtSlot()
    def disconnect_stage(self):
        if self.state != 'idle':
            return
        driver, self.driver = self.driver, None
        try:
            if driver:
                driver.stop()
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            if driver:
                try:
                    driver.close()
                except Exception as exc:
                    self.error.emit(str(exc))
            self.stage_changed.emit(False, {})

    @pyqtSlot(object)
    def start_scan(self, config):
        if self.state != 'idle' or self.driver is None or self.shutting_down:
            self.error.emit('Подключите ESP301 и дождитесь завершения текущей операции')
            self.state_changed.emit(self.state, 'Запуск отклонён: проверьте подключение и состояние оси')
            return
        try:
            targets = config.positions()
            left, right = self.driver.limits
            if min(targets) < left or max(targets) > right:
                raise ValueError(f'Диапазон сканирования выходит за пределы [{left}, {right}] mm')
            if not self.driver.motion_done():
                raise ValueError('Ось уже движется. Остановите её перед запуском.')
            self.driver.check_error()
            self.config, self.targets, self.index = config, targets, 0
            self.cancel_event.clear()
            self.stop_requested = self.pause_requested = False
            self.stage_stopped = True
            self.outcome, self.message = 'complete', ''
            self.set_state('preparing', 'Подготовка локина и файла данных…')
            self.lease = self.sequence+1
            self.ask_lockin('prepare', {})
        except Exception as exc:
            self.config = None
            self.error.emit(str(exc))
            self.set_state('idle', 'Запуск отменён')

    def ask_lockin(self, operation, payload):
        self.sequence += 1
        self.awaiting = (operation, self.sequence, time.monotonic()+30)
        self.lockin_request.emit(operation, self.sequence, dict(payload, lease=self.lease))

    @pyqtSlot(str, int, object)
    def lockin_reply(self, operation, token, reply):
        if self.awaiting is None or self.awaiting[:2] != (operation, token):
            return  # A late USB reply from an aborted request cannot become a new point.
        self.awaiting = None
        try:
            if operation == 'release':
                if not reply['ok']:
                    self.outcome = 'error'
                    self.message += '\n'+reply.get('error', 'Не удалось восстановить настройки локина')
                self.lease = 0
                self.finalize()
                return
            if not reply['ok']:
                raise RuntimeError(reply.get('error', 'Ошибка чтения локина'))
            if operation == 'prepare':
                if self.stop_requested or self.cancel_event.is_set():
                    if not self.stop_requested:
                        self.stop()
                    self.try_finish(); return
                metadata = dict(stage=self.info, lockin=reply['result'])
                self.writer = ScanWriter(self.config, metadata)
                path = self.writer.open()
                self.run_started.emit(dict(count=len(self.targets), path=path, metadata=metadata))
                if self.pause_requested:
                    self.set_state('paused', 'Пауза перед первой точкой')
                else:
                    self.move_current()
            elif operation == 'sample':
                if self.stop_requested or self.cancel_event.is_set():
                    if not self.stop_requested:
                        self.stop()
                    self.try_finish(); return
                data = reply['result']
                bad = data['overload'] or data['status'] & 0x58
                if bad:
                    warning = (f'Point {self.index}, position {self.actual:.9f} mm: '
                               f'ST=0x{data["status"]:02X}, OV=0x{data["overload"]:02X}')
                    self.writer.event(warning)
                    if self.config.stop_on_warning:
                        raise RuntimeError('Перегрузка или потеря захвата локина: '+warning)
                self.writer.append(self.actual, data['values'])
                self.point_ready.emit(self.index, [self.actual, *data['values']])
                self.index += 1
                if self.index == len(self.targets):
                    self.stop_requested = True
                    self.try_finish()
                elif self.pause_requested:
                    self.set_state('paused', f'Пауза · сохранено {self.index} точек')
                else:
                    self.move_current()
        except Exception as exc:
            self.fail(str(exc), isinstance(exc, ESPConnectionError))

    def move_current(self):
        if self.cancel_event.is_set():
            if not self.stop_requested:
                self.stop()
            return
        self.driver.move_absolute(self.targets[self.index])
        self.stage_stopped = False
        self.deadline = time.monotonic()+self.config.motion_timeout_s
        self.set_state('moving', f'Переход к точке {self.index+1}/{len(self.targets)}')

    @pyqtSlot()
    def pause_or_resume(self):
        if self.config is None or self.stop_requested:
            return
        try:
            if self.state == 'paused':
                self.pause_requested = False
                self.move_current()  # Reposition and repeat settling after a motion pause.
            else:
                self.pause_requested = True
                if self.state == 'moving':
                    self.driver.stop()
                    self.set_state('pausing', 'Остановка оси для паузы…')
                elif self.state == 'settling':
                    self.set_state('paused', 'Пауза · выдержка будет повторена после продолжения')
        except Exception as exc:
            self.fail(str(exc), isinstance(exc, ESPConnectionError))

    @pyqtSlot()
    def stop(self):
        if self.stop_requested:
            return
        if self.state == 'idle' and self.driver is None:
            return
        self.outcome = 'stopped'
        self.stop_requested = True
        self.begin_stop()

    @pyqtSlot()
    def reset(self):
        if self.state == 'idle':
            self.finished.emit('reset', 'Графики очищены; сохранённые файлы не изменены')
        else:
            if self.stop_requested:
                if self.outcome != 'error':
                    self.outcome = 'reset'
                return
            self.outcome = 'reset'
            self.stop_requested = True
            self.begin_stop()

    def begin_stop(self):
        try:
            if self.driver:
                self.driver.stop()
                self.stage_stopped = False
                self.deadline = time.monotonic()+10
                self.set_state('stopping', 'Остановка оси и завершение записи…')
            else:
                self.stage_stopped = True
            self.try_finish()
        except Exception as exc:
            self.message += '\nНе удалось подтвердить остановку оси: '+str(exc)
            self.outcome = 'error'
            self.drop_stage()
            self.stage_stopped = True
            self.try_finish()

    def try_finish(self):
        if not self.stop_requested or not self.stage_stopped or self.awaiting is not None:
            return
        if self.lease:
            self.set_state('finishing', 'Восстановление настроек локина и закрытие файлов…')
            self.ask_lockin('release', {})
        else:
            self.finalize()

    def finalize(self):
        was_scan = self.config is not None
        if self.writer:
            try:
                self.writer.close(self.outcome, self.message)
            except Exception as exc:
                self.outcome = 'error'; self.message += '\n'+str(exc)
            self.writer = None
        message = self.message.strip() or f'Сохранено точек: {self.index}/{len(self.targets)}'
        self.config = None
        self.stop_requested = self.pause_requested = False
        self.set_state('idle', ('Ошибка: '+message) if self.outcome == 'error' else 'Готово')
        if was_scan:
            self.finished.emit(self.outcome, message)
        if self.shutting_down:
            self.shutdown_now()

    def fail(self, message, connection_lost=False):
        self.error.emit(message)
        self.message += '\n'+message
        self.outcome = 'error'; self.stop_requested = True
        if connection_lost:
            self.message += '\nОстановка оси не подтверждена: проверьте контроллер ESP301.'
            # Best effort only: a broken cable cannot guarantee a physical stop.
            try:
                self.driver.stop()
            except Exception:
                pass
            self.drop_stage(); self.stage_stopped = True
            self.try_finish()
        else:
            self.begin_stop()

    def drop_stage(self):
        if self.driver:
            try:
                self.driver.close()
            except Exception:
                pass
        self.driver = None
        self.stage_changed.emit(False, {})

    @pyqtSlot(float)
    def home(self, timeout):
        if self.state != 'idle' or self.driver is None:
            return
        try:
            if not self.driver.motion_done():
                raise ESPError('Ось уже движется')
            self.driver.home()
            self.stage_stopped = False
            self.deadline = time.monotonic()+timeout
            self.set_state('homing', 'Поиск Home по настройкам контроллера…')
        except Exception as exc:
            self.fail(str(exc), isinstance(exc, ESPConnectionError))

    @pyqtSlot()
    def tick(self):
        try:
            now = time.monotonic()
            if self.awaiting and now > self.awaiting[2]:
                operation = self.awaiting[0]
                self.awaiting = None
                if operation == 'release':
                    self.lease = 0
                    self.outcome = 'error'; self.message += '\nТайм-аут восстановления локина'
                    self.finalize()
                else:
                    self.fail('Тайм-аут запроса локина: '+operation)
                return
            if self.driver is None:
                return
            if self.state in ('moving', 'pausing', 'stopping', 'homing'):
                done = self.driver.motion_done()
                if done or now-self.last_position_time >= .1:
                    self.actual = self.driver.position()
                    self.position_changed.emit(self.actual)
                    self.last_position_time = now
                if not done:
                    if now > self.deadline:
                        if self.state == 'stopping':
                            self.outcome = 'error'
                            self.message += '\nОстановка оси не подтверждена. Проверьте контроллер.'
                            self.error.emit(self.message.strip())
                            self.drop_stage(); self.stage_stopped = True
                            self.try_finish(); return
                        raise ESPError('Тайм-аут движения/остановки оси')
                    return
                self.stage_stopped = True
                self.driver.check_error()
                if self.state == 'stopping':
                    self.try_finish()
                elif self.state == 'pausing':
                    self.set_state('paused', 'Ось остановлена · пауза')
                elif self.state == 'homing':
                    self.set_state('idle', 'Home выполнен')
                else:
                    if abs(self.actual-self.targets[self.index]) > self.config.tolerance_mm:
                        raise ESPError(f'Позиция {self.actual:.9f} mm не совпала с целью {self.targets[self.index]:.9f} mm')
                    self.deadline = now+self.config.delay_ms/1000
                    self.set_state('settling', f'Выдержка после остановки · точка {self.index+1}/{len(self.targets)}')
            if self.state == 'settling' and now >= self.deadline:
                if self.cancel_event.is_set():
                    self.stop(); return
                self.actual = self.driver.position()
                if not self.driver.motion_done() or abs(self.actual-self.targets[self.index]) > self.config.tolerance_mm:
                    raise ESPError('Линия задержки сместилась во время выдержки')
                self.position_changed.emit(self.actual)
                self.set_state('sampling', f'Измерение · точка {self.index+1}/{len(self.targets)}')
                self.ask_lockin('sample', dict(position=self.actual))
            elif self.state in ('idle', 'paused') and now-self.last_position_time >= .5:
                self.actual = self.driver.position()
                self.position_changed.emit(self.actual)
                self.last_position_time = now
        except Exception as exc:
            self.fail(str(exc), isinstance(exc, ESPConnectionError))

    @pyqtSlot()
    def shutdown(self):
        self.shutting_down = True
        if self.state == 'idle' and self.awaiting is None and not self.lease:
            self.shutdown_now()
        else:
            self.outcome = 'closed'; self.stop_requested = True
            self.begin_stop()

    def shutdown_now(self):
        if self.timer:
            self.timer.stop()
        driver, self.driver = self.driver, None
        try:
            if driver:
                driver.stop()
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            if driver:
                try:
                    driver.close()
                except Exception as exc:
                    self.error.emit(str(exc))
            self.stopped.emit()
