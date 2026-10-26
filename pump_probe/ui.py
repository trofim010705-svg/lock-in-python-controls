"""Two-tab experiment application. Plot drawing is throttled independently of I/O."""
from datetime import datetime
from pathlib import Path
import numpy as np
from PyQt5.QtCore import QThread, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QSizePolicy, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)
from gui_widgets import InstrumentTabBar
import pyqtgraph as pg  # PyQt5 is imported first so pyqtgraph uses the same Qt binding.
from .data import ScanConfig
from .experiment import ExperimentWorker
from .lockin_widget import LockInWidget


STYLE = '''
QMainWindow { background: #edf2f8; }
QGroupBox { background: white; border: 1px solid #dce4ef; border-radius: 9px;
            margin-top: 11px; padding: 14px 10px 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; color: #26496d;
                  padding: 0 4px; font-weight: 600; }
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox { min-height: 27px; }
QPushButton { padding: 8px 13px; border-radius: 5px; border: 1px solid #c6d3e2;
              background: #f5f8fc; color: #173c61; }
QPushButton:disabled { color: #91a0ae; background: #eef1f5; }
QPushButton#primary { color: white; background: #1264a3; border: 1px solid #1264a3; }
QPushButton#danger { color: #a52323; background: #fff1ef; border-color: #e7b7b1; }
QLabel#position { color: #0d5076; font-size: 30px; font-weight: 600; padding: 8px 0; }
QProgressBar { border: 1px solid #d7e1ed; border-radius: 4px; text-align: center; min-height: 20px; }
QProgressBar::chunk { background: #229c9d; }
'''


class PumpProbeWidget(QWidget):
    connect_requested = pyqtSignal(object)
    disconnect_requested = pyqtSignal()
    start_requested = pyqtSignal(object)
    pause_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    reset_requested = pyqtSignal()
    home_requested = pyqtSignal(float)
    goto_requested = pyqtSignal(float, float, float)

    def __init__(self, demo=False):
        super().__init__()
        self.demo, self.stage_connected, self.lockin_connected = demo, False, False
        pg.setConfigOption('foreground', '#42566c')
        self.state = 'idle'
        self.rows = np.empty((0, 5))
        self.count = 0
        self.plot_dirty = False
        self.output_is_default = True
        root = QVBoxLayout(self)
        title = QLabel('PUMP–PROBE  /  SPIN DYNAMICS')
        title.setStyleSheet('font-size: 18px; font-weight: 600; color: #164367; padding: 4px;')
        root.addWidget(title)
        self.splitter = splitter = QSplitter(Qt.Horizontal); root.addWidget(splitter, 1)
        splitter.setChildrenCollapsible(False)
        settings = QWidget(); left = QVBoxLayout(settings); left.setContentsMargins(0, 0, 8, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(settings)
        self.settings_scroll = scroll
        scroll.setMinimumWidth(500); splitter.addWidget(scroll)
        connection = QGroupBox('NEWPORT ESP301 · USB / COM'); form = QFormLayout(connection)
        self.port = QComboBox(); self.port.setEditable(True)
        self.port.setMinimumContentsLength(12)
        self.port.lineEdit().setPlaceholderText("COM-порт ESP301")
        self.port.setToolTip("Если список пуст: проверьте USB-драйвер ESP301 в Диспетчере устройств Windows, затем нажмите обновление.")
        self.refresh_ports_button = QPushButton('↻')
        self.refresh_ports_button.setToolTip('Обновить список COM-портов')
        row = QHBoxLayout(); row.addWidget(self.port, 1); row.addWidget(self.refresh_ports_button)
        form.addRow('COM-порт', row)
        self.axis = QSpinBox(); self.axis.setRange(1, 3)
        form.addRow('Ось', self.axis)
        self.baudrate = QComboBox()
        for baud in (921600, 19200, 115200, 57600, 38400, 9600):
            self.baudrate.addItem(str(baud), baud)
        self.baudrate.setToolTip('USB ESP301: 921600, 8N1. Для RS232 используйте настройки контроллера.')
        form.addRow('Baud, 8N1', self.baudrate)
        self.connect_button = QPushButton('Подключить')
        self.connect_button.clicked.connect(self.toggle_connection)
        form.addRow(self.connect_button)
        self.stage_label = QLabel('Линия задержки не подключена'); self.stage_label.setWordWrap(True)
        form.addRow(self.stage_label)
        self.position = QLabel('— mm'); self.position.setObjectName('position')
        form.addRow(QLabel('Текущая позиция')); form.addRow(self.position)
        row = QHBoxLayout()
        self.home_button, self.stop_button = QPushButton('⌂ Home'), QPushButton('■ Стоп')
        self.stop_button.setObjectName('danger')
        self.stop_button.setToolTip('Остановить выбранную ось командой ST и завершить текущий скан с сохранением данных.')
        self.home_button.setToolTip('Поиск Home командой OR, с режимом и скоростью, настроенными на контроллере.')
        self.home_button.clicked.connect(lambda: self.home_requested.emit(self.motion_timeout.value()))
        self.stop_button.clicked.connect(self.stop_requested)
        row.addWidget(self.home_button); row.addWidget(self.stop_button); form.addRow(row)
        self.goto_position = self.spin(-1e6, 1e6, 3, 0)
        self.goto_position.setSingleStep(.001)
        self.goto_button = QPushButton('Go to')
        self.goto_button.setToolTip('Переместить ось к указанной абсолютной позиции без измерений и записи файла.')
        self.goto_button.clicked.connect(self.go_to)
        row = QHBoxLayout(); row.addWidget(self.goto_position, 1); row.addWidget(self.goto_button)
        form.addRow('Перейти к, mm', row)
        left.addWidget(connection)
        scan = QGroupBox('СКАНИРОВАНИЕ'); form = QFormLayout(scan)
        self.start_position = self.spin(-1e6, 1e6, 3, 0)
        self.stop_position = self.spin(-1e6, 1e6, 3, 1)
        self.step = self.spin(.001, 1e6, 3, .01)
        for w in (self.start_position, self.stop_position, self.step):
            w.setSingleStep(.001)
        self.delay = self.spin(0, 600000, 1, 50)
        for text, w in [('Старт, mm', self.start_position), ('Стоп, mm', self.stop_position),
                        ('Шаг, mm', self.step), ('Задержка, ms', self.delay)]:
            form.addRow(text, w)
        hint = QLabel('Задержка отсчитывается после остановки оси.\nЛокин должен работать в режиме Dual reference.')
        hint.setWordWrap(True); hint.setStyleSheet('color: #65768a; font-size: 11px;')
        form.addRow(hint)
        self.tolerance = self.spin(.001, 1, 3, .002)
        self.tolerance.setSingleStep(.001)
        self.motion_timeout = self.spin(1, 3600, 0, 120)
        form.addRow('Допуск позиции, mm', self.tolerance)
        form.addRow('Тайм-аут движения, s', self.motion_timeout)
        self.stop_on_warning = QCheckBox('Стоп при перегрузке / потере захвата')
        self.stop_on_warning.setChecked(True)
        form.addRow(self.stop_on_warning)
        row = QHBoxLayout()
        self.start_button, self.pause_button, self.reset_button = QPushButton('▶ Старт'), QPushButton('Ⅱ Пауза'), QPushButton('Сброс')
        self.start_button.setObjectName('primary')
        self.reset_button.setToolTip('Остановить эксперимент и очистить графики. Сохранённые файлы не удаляются.')
        self.start_button.clicked.connect(self.start_scan)
        self.pause_button.clicked.connect(self.pause_requested)
        self.reset_button.clicked.connect(self.reset_requested)
        for b in (self.start_button, self.pause_button, self.reset_button): row.addWidget(b)
        form.addRow(row)
        self.progress = QProgressBar(); self.progress.setRange(0, 1); self.progress.setValue(0)
        form.addRow(self.progress)
        self.lockin_label = QLabel('Lock-in: не подключён'); form.addRow(self.lockin_label)
        left.addWidget(scan)
        save = QGroupBox('ДАННЫЕ И КОММЕНТАРИИ'); form = QVBoxLayout(save)
        row = QHBoxLayout()
        self.output = QLineEdit(self.default_output())
        self.output.textEdited.connect(lambda _: setattr(self, 'output_is_default', False))
        self.browse_button = QPushButton('…'); self.browse_button.clicked.connect(self.choose_output)
        row.addWidget(self.output); row.addWidget(self.browse_button); form.addLayout(row)
        self.comments = QPlainTextEdit(); self.comments.setPlaceholderText('Образец, температура, поле, мощность pump/probe…')
        self.comments.setMaximumHeight(110); form.addWidget(self.comments)
        self.saved_path = QLabel('Файл: —'); self.saved_path.setWordWrap(True); self.saved_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.saved_path.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        form.addWidget(self.saved_path)
        left.addWidget(save); left.addStretch()
        charts = QWidget(); chart_layout = QVBoxLayout(charts); chart_layout.setContentsMargins(4, 0, 0, 0)
        self.graphs = pg.GraphicsLayoutWidget()
        self.graphs.setBackground('#ffffff')
        self.plots, self.curves = [], []
        for index, (name, color) in enumerate(zip(('X1', 'Y1', 'X2', 'Y2'), ('#1264a3', '#119c9b', '#7956b1', '#dc8a25'))):
            plot = self.graphs.addPlot(row=index//2, col=index%2, title=name)
            plot.setTitle(name, color='#294263', size='13pt')
            plot.showGrid(x=True, y=True, alpha=.15)
            # Coordinates are in millimetres, so use a plain 'mm' label and disable axis prefix scaling.
            plot.setLabel('bottom', 'Позиция линии задержки', units='mm')
            plot.getAxis('bottom').enableAutoSIPrefix(False)
            plot.setLabel('left', name, units='V')
            plot.getViewBox().setDefaultPadding(.05)
            curve = plot.plot(pen=pg.mkPen(color, width=2), symbol='o', symbolSize=4,
                              symbolBrush=color, symbolPen=None)
            curve.setDownsampling(auto=True, method='peak'); curve.setClipToView(True)
            if index:
                plot.setXLink(self.plots[0])
            self.plots.append(plot); self.curves.append(curve)
        chart_layout.addWidget(self.graphs, 1)
        self.status = QLabel('Подключите оба прибора, выберите диапазон и нажмите «Старт».')
        self.status.setWordWrap(True); self.status.setMinimumHeight(48)
        self.status.setStyleSheet('padding: 10px; background: #e7eff8; color: #164367; border-radius: 6px;')
        chart_layout.addWidget(self.status)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(500)
        self.log.setMaximumHeight(105); chart_layout.addWidget(self.log)
        splitter.addWidget(charts); splitter.setStretchFactor(0, 0); splitter.setStretchFactor(1, 1)
        splitter.setSizes([560, 920])
        for form in settings.findChildren(QFormLayout):
            form.setRowWrapPolicy(QFormLayout.WrapLongRows)
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.plot_timer = QTimer(self); self.plot_timer.setInterval(50); self.plot_timer.timeout.connect(self.redraw)
        self.plot_timer.start()
        self.refresh_ports_button.clicked.connect(self.refresh_ports)
        self.refresh_ports(); self.update_enabled()

    @staticmethod
    def spin(low, high, decimals, value):
        w = QDoubleSpinBox(); w.setDecimals(decimals); w.setRange(low, high)
        w.setKeyboardTracking(False); w.setValue(value)
        return w

    @staticmethod
    def default_output():
        return str(Path.cwd()/'data'/('pump_probe_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.dat'))

    def refresh_ports(self):
        from serial.tools import list_ports
        selected = self.port.currentText()
        self.port.clear()
        if self.demo:
            self.port.addItem('DEMO')
        for p in list_ports.comports():
            self.port.addItem(p.device)
            self.port.setItemData(self.port.count()-1, p.description, Qt.ToolTipRole)
        if selected:
            self.port.setCurrentText(selected)

    def toggle_connection(self):
        if self.stage_connected:
            self.connect_button.setEnabled(False)
            self.start_button.setEnabled(False); self.home_button.setEnabled(False)
            self.disconnect_requested.emit()
        elif not self.port.currentText().strip():
            self.show_error('Выберите COM-порт ESP301')
        else:
            self.connect_button.setEnabled(False)
            self.connect_requested.emit(dict(port=self.port.currentText().strip(), axis=self.axis.value(),
                                             baudrate=self.baudrate.currentData()))

    def choose_output(self):
        filename, _ = QFileDialog.getSaveFileName(self, 'Файл данных эксперимента', self.output.text(),
                                                'Data (*.dat);;Text (*.txt)')
        if filename:
            if not Path(filename).suffix:
                filename += '.dat'
            self.output.setText(filename); self.output_is_default = False

    def start_scan(self):
        try:
            config = ScanConfig(self.start_position.value(), self.stop_position.value(), self.step.value(),
                                self.delay.value(), self.output.text().strip(), self.comments.toPlainText(),
                                self.tolerance.value(), self.motion_timeout.value(), self.stop_on_warning.isChecked())
            config.point_count()
            if not config.output:
                raise ValueError('Выберите путь к файлу данных')
            self.set_state('preparing', 'Подготовка сканирования…')
            self.start_requested.emit(config)
        except Exception as exc:
            self.show_error(str(exc))

    def go_to(self):
        if self.state != 'idle' or not self.stage_connected:
            return
        self.set_state('positioning', 'Перемещение без измерений…')
        self.goto_requested.emit(self.goto_position.value(), self.tolerance.value(), self.motion_timeout.value())

    @pyqtSlot(bool, object)
    def set_stage(self, connected, info):
        self.stage_connected = connected
        if connected:
            limits = info['limits']
            self.stage_label.setText(f'{info["version"]}\nОсь {info["axis"]} · {info["stage"]}\n'
                                     f'Пределы: {limits[0]:g} … {limits[1]:g} mm')
            for w in (self.start_position, self.stop_position, self.goto_position):
                w.setRange(*limits)
            if info.get('history'):
                self.log.appendPlainText('Предыдущие ошибки ESP301: '+'; '.join(info['history']))
        else:
            self.stage_label.setText('Линия задержки не подключена'); self.position.setText('— mm')
        self.update_enabled()

    @pyqtSlot(bool)
    def set_lockin(self, connected):
        self.lockin_connected = connected
        self.lockin_label.setText('Lock-in: подключён' if connected else 'Lock-in: не подключён')
        self.update_enabled()

    @pyqtSlot(float)
    def set_position(self, position):
        self.position.setText(f'{position:.3f} mm')

    @pyqtSlot(str, str)
    def set_state(self, state, message):
        self.state = state
        self.status.setText(message)
        self.status.setStyleSheet('padding: 10px; background: #e7eff8; color: #164367; border-radius: 6px;')
        self.update_enabled()

    def update_enabled(self):
        idle = self.state == 'idle'
        self.connect_button.setText('Отключить' if self.stage_connected else 'Подключить')
        self.connect_button.setEnabled(idle)
        for w in (self.port, self.axis, self.baudrate, self.refresh_ports_button):
            w.setEnabled(idle and not self.stage_connected)
        for w in (self.start_position, self.stop_position, self.step, self.delay, self.output, self.comments,
                  self.browse_button, self.tolerance, self.motion_timeout, self.stop_on_warning):
            w.setEnabled(idle)
        self.home_button.setEnabled(idle and self.stage_connected)
        self.goto_button.setEnabled(idle and self.stage_connected)
        self.goto_position.setEnabled(idle and self.stage_connected)
        self.stop_button.setEnabled(self.stage_connected or not idle)
        self.start_button.setEnabled(idle and self.stage_connected and self.lockin_connected)
        self.pause_button.setEnabled(self.state in ('preparing', 'moving', 'settling', 'sampling', 'paused'))
        self.pause_button.setText('▶ Продолжить' if self.state == 'paused' else 'Ⅱ Пауза')
        self.reset_button.setEnabled(self.state not in ('stopping', 'finishing'))

    @pyqtSlot(object)
    def begin_plot(self, info):
        self.rows = np.empty((info['count'], 5), dtype=float); self.count = 0
        self.progress.setRange(0, info['count']); self.progress.setValue(0)
        for plot, name in zip(self.plots, ('X1', 'Y1', 'X2', 'Y2')):
            plot.setLabel('left', name, units=info['metadata']['lockin']['unit'])
        for curve in self.curves: curve.setData([], [])
        self.saved_path.setText('Файл: '+info['path'])
        self.log.appendPlainText('Запись: '+info['path']+'\nЧтение локина: '+info['metadata']['lockin']['acquisition'])

    @pyqtSlot(int, object)
    def add_point(self, index, row):
        if index != self.count or index >= len(self.rows):
            self.show_error('Нарушен порядок точек измерения'); return
        self.rows[index] = row; self.count += 1
        self.progress.setValue(self.count); self.plot_dirty = True

    def redraw(self):
        if self.plot_dirty:
            data = self.rows[:self.count]
            if len(data) > 1 and data[0, 0] > data[-1, 0]:
                data = data[::-1]  # pyqtgraph clipping/downsampling expects increasing X.
            for index, curve in enumerate(self.curves):
                curve.setData(data[:, 0], data[:, index+1])
            self.plot_dirty = False

    @pyqtSlot(str, str)
    def finish(self, outcome, message):
        self.redraw()
        self.log.appendPlainText(f'{outcome}: {message}')
        self.status.setText(message)
        if outcome == 'error':
            self.show_error(message)
        elif outcome == 'reset':
            self.rows = np.empty((0, 5)); self.count = 0; self.progress.setValue(0)
            for curve in self.curves: curve.setData([], [])
        if self.output_is_default:
            self.output.setText(self.default_output())

    @pyqtSlot(str)
    def show_error(self, message):
        self.status.setText(message)
        self.status.setStyleSheet('padding: 10px; color: #a52323; background: #fff0ed; border-radius: 6px;')
        self.log.appendPlainText('Ошибка: '+message)
        self.connect_button.setEnabled(self.state == 'idle')


class ExperimentWindow(QMainWindow):
    shutdown_stage = pyqtSignal()

    def __init__(self, args, driver_factory=None):
        super().__init__()
        self.setWindowTitle('Pump–Probe Lab · Signal Recovery 7230 + Newport ESP301'+(' · DEMO' if args.demo else ''))
        self.resize(1480, 980); self.setStyleSheet(STYLE)
        self.closing = self.stage_stopped = False
        self.tabs = QTabWidget(); self.setCentralWidget(self.tabs)
        self.tabs.setTabBar(InstrumentTabBar(self.tabs))
        self.lockin = LockInWidget(args)
        self.pump = PumpProbeWidget(args.demo)
        self.tabs.addTab(self.lockin, 'Управление Lock-in')
        self.tabs.addTab(self.pump, 'Pump-Probe и Линия задержки')
        self.stage_thread = QThread(self)
        self.worker = ExperimentWorker(args.demo, driver_factory)
        self.worker.moveToThread(self.stage_thread)
        self.stage_thread.started.connect(self.worker.initialize)
        self.pump.connect_requested.connect(self.worker.connect_stage)
        self.pump.disconnect_requested.connect(self.worker.disconnect_stage)
        self.pump.start_requested.connect(self.worker.start_scan)
        self.pump.pause_requested.connect(self.worker.pause_or_resume)
        # Only the thread-safe cancellation flag is set directly from the GUI.
        # Serial I/O and all state transitions stay in the worker thread.
        self.pump.stop_requested.connect(self.worker.cancel_event.set, Qt.DirectConnection)
        self.pump.stop_requested.connect(self.worker.stop)
        self.pump.reset_requested.connect(self.worker.cancel_event.set, Qt.DirectConnection)
        self.pump.reset_requested.connect(self.worker.reset)
        self.pump.home_requested.connect(self.worker.home)
        self.pump.goto_requested.connect(self.worker.go_to)
        self.worker.stage_changed.connect(self.pump.set_stage)
        self.worker.position_changed.connect(self.pump.set_position)
        self.worker.state_changed.connect(self.pump.set_state)
        self.worker.run_started.connect(self.pump.begin_plot)
        self.worker.point_ready.connect(self.pump.add_point)
        self.worker.finished.connect(self.pump.finish)
        self.worker.error.connect(self.pump.show_error)
        self.lockin.connection_changed.connect(self.pump.set_lockin)
        self.worker.lockin_request.connect(self.lockin.handle_experiment_request)
        self.lockin.experiment_reply.connect(self.worker.lockin_reply)
        self.shutdown_stage.connect(self.worker.cancel_event.set, Qt.DirectConnection)
        self.shutdown_stage.connect(self.worker.shutdown)
        self.worker.stopped.connect(self.stage_thread.quit)
        self.stage_thread.finished.connect(self.worker.deleteLater)
        self.stage_thread.finished.connect(self.stage_finished)
        self.lockin.thread.finished.connect(self.lockin_finished)
        self.stage_thread.start()
        if args.demo:
            QTimer.singleShot(0, self.connect_demo)

    def connect_demo(self):
        self.lockin.submit('open'); self.pump.toggle_connection(); self.tabs.setCurrentIndex(1)

    def closeEvent(self, event):
        if self.stage_stopped and self.lockin.thread_stopped:
            event.accept(); return
        event.ignore()
        if not self.closing:
            self.closing = True; self.tabs.setEnabled(False)
            self.statusBar().showMessage('Остановка оси, завершение записи и закрытие приборов…')
            self.shutdown_stage.emit()

    @pyqtSlot()
    def stage_finished(self):
        self.stage_stopped = True
        self.lockin.close()
        self.lockin_finished()

    @pyqtSlot()
    def lockin_finished(self):
        if self.closing and self.stage_stopped and self.lockin.thread_stopped:
            self.close()
