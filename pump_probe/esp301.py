"""Newport ESP301 serial driver. Commands follow ESP301 manual EDH0235En1030.

This driver has no Qt dependencies. One worker thread owns each instance.
USB serial is 921600 baud, 8N1; commands end in CR, responses in CRLF.
"""
import math
import time


class ESPError(RuntimeError):
    pass


class ESPConnectionError(ESPError):
    pass


class ESP301:
    def __init__(self, port, axis=1, baudrate=921600, timeout=0.25, serial_factory=None):
        if axis not in (1, 2, 3):
            raise ValueError('Номер оси должен быть 1, 2 или 3')
        self.port, self.axis, self.baudrate = port, axis, baudrate
        self.timeout, self.serial_factory = timeout, serial_factory
        self.serial = None
        self.limits = None

    def open(self):
        if self.serial is not None:
            raise ESPError('COM-порт уже открыт')
        if self.serial_factory is None:
            from serial import Serial
            factory = Serial
        else:
            factory = self.serial_factory
        try:
            self.serial = factory(port=self.port, baudrate=self.baudrate, bytesize=8,
                                  parity='N', stopbits=1, timeout=self.timeout,
                                  write_timeout=self.timeout, xonxoff=False, rtscts=False, dsrdtr=False)
            self.serial.reset_input_buffer()
            version = self.query('VE?')
            if 'ESP301' not in version.upper():
                raise ESPError('Ответ не похож на Newport ESP301: '+version)
            history = self.errors()
            units = int(self.numeric(f'{self.axis}SN?'))
            if units != 2:
                raise ESPError(f'Ось {self.axis}: SN={units}. Требуется ось, настроенная в миллиметрах (SN=2). '
                               'Настройки масштаба автоматически не изменяются.')
            self.limits = (self.numeric(f'{self.axis}SL?'), self.numeric(f'{self.axis}SR?'))
            if self.limits[0] > self.limits[1]:
                raise ESPError('Некорректные программные пределы SL/SR')
            info = dict(version=version, axis=self.axis, port=self.port, baudrate=self.baudrate,
                        stage=self.query(f'{self.axis}ID?'), limits=self.limits,
                        motor_on=bool(self.numeric(f'{self.axis}MO?')), position=self.position(), history=history)
            self.check_error()
            return info
        except Exception:
            self.close()
            raise

    def send(self, command):
        if self.serial is None:
            raise ESPConnectionError('ESP301 не подключён')
        if any(c in command for c in '\r\n\x00'):
            raise ValueError('Команда должна содержать одну ASCII-строку')
        try:
            packet = (command+'\r').encode('ascii')
            written = self.serial.write(packet)
            if written != len(packet):
                raise ESPConnectionError('Не удалось полностью отправить команду '+command)
        except (OSError, TimeoutError) as exc:
            raise ESPConnectionError(f'Ошибка записи {command}: {exc}') from exc

    def query(self, command):
        self.send(command)
        try:
            # Some serial adapters echo the command; it is not a numeric reply.
            for _ in range(3):
                raw = self.serial.read_until(b'\n', size=4096)
                if not raw.endswith(b'\r\n'):
                    raise ESPConnectionError(f'Тайм-аут/неполный ответ на {command}: {raw[:80]!r}')
                response = raw.decode('ascii').strip()
                if response and response.upper() != command.upper():
                    return response
            raise ESPConnectionError('Нет ответа на '+command)
        except (OSError, UnicodeError) as exc:
            raise ESPConnectionError(f'Ошибка чтения {command}: {exc}') from exc

    def numeric(self, command):
        try:
            value = float(self.query(command))
        except ValueError as exc:
            raise ESPConnectionError('Ожидался числовой ответ на '+command) from exc
        if not math.isfinite(value):
            raise ESPConnectionError('Некорректное число в ответе на '+command)
        return value

    def errors(self):
        errors = []
        for _ in range(11):
            response = self.query('TB?')
            try:
                code = int(response.split(',', 1)[0])
            except ValueError as exc:
                raise ESPConnectionError('Некорректный ответ TB?: '+response) from exc
            if code == 0:
                return errors
            errors.append(response)
        raise ESPError('Очередь ошибок ESP301 не освобождается: '+'; '.join(errors))

    def check_error(self):
        errors = self.errors()
        if errors:
            raise ESPError('; '.join(errors))

    def position(self):
        # TP has no '?' in the manual, unlike MD?, SL? and SR?.
        return self.numeric(f'{self.axis}TP')

    def motion_done(self):
        value = self.numeric(f'{self.axis}MD?')
        if value not in (0, 1):
            raise ESPConnectionError('Неверный ответ MD?: '+str(value))
        return bool(value)

    def enable_motor(self):
        self.check_error()
        if not self.numeric(f'{self.axis}MO?'):
            self.send(f'{self.axis}MO')
            self.check_error()

    def move_absolute(self, position):
        if not math.isfinite(position) or self.limits is None or not self.limits[0] <= position <= self.limits[1]:
            raise ESPError(f'Позиция {position} mm вне программных пределов {self.limits}')
        self.enable_motor()
        self.send(f'{self.axis}PA{position:.9f}')
        self.check_error()

    def home(self):
        self.enable_motor()
        # Respect the home-search mode previously configured with OM.
        self.send(f'{self.axis}OR')
        self.check_error()

    def stop(self):
        self.send(f'{self.axis}ST')  # Controlled deceleration of the selected axis only.

    def close(self):
        connection, self.serial = self.serial, None
        if connection is not None:
            connection.close()


class DemoESP301:
    """Deterministic moving stage for GUI previews and hardware-free tests."""
    def __init__(self, port='DEMO', axis=1, baudrate=921600, **kwargs):
        self.port, self.axis, self.baudrate = port, axis, baudrate
        self.limits = (-25.0, 25.0)
        self.opened = False
        self._position = self._start = self._target = 0.0
        self._started = self._ends = 0.0
        self.commands = []

    def open(self):
        self.opened = True
        return dict(version='ESP301 DEMO', axis=self.axis, port=self.port, baudrate=self.baudrate,
                    stage='Simulated linear stage', limits=self.limits, motor_on=True,
                    position=self.position(), history=[])

    def position(self):
        if not self.opened:
            raise ESPConnectionError('Demo stage is closed')
        now = time.monotonic()
        fraction = min(1., (now-self._started)/max(1e-9, self._ends-self._started))
        self._position = self._start+(self._target-self._start)*fraction
        return self._position

    def motion_done(self):
        self.position()
        return time.monotonic() >= self._ends

    def move_absolute(self, position):
        if not math.isfinite(position) or not self.limits[0] <= position <= self.limits[1]:
            raise ESPError('Demo stage travel limit exceeded')
        self._start, self._target = self.position(), position
        self._started = time.monotonic()
        self._ends = self._started+max(.005, abs(self._target-self._start)/20)
        self.commands.append(('PA', position))

    def home(self):
        self.move_absolute(0)
        self.commands.append(('OR',))

    def stop(self):
        self._position = self.position()
        self._start = self._target = self._position
        self._ends = self._started = time.monotonic()
        self.commands.append(('ST',))

    def check_error(self):
        pass

    def close(self):
        self.opened = False
