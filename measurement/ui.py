"""Existing instrument controls plus timed Magn1/Phase1 acquisition."""
from pathlib import Path
from threading import Event
import time
from PyQt5.QtCore import Qt,QTimer,QStandardPaths,pyqtSlot
from PyQt5.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QGroupBox,
    QLabel,QDoubleSpinBox,QLineEdit,QPlainTextEdit,QPushButton,QFileDialog,
    QTabWidget,QSplitter,QScrollArea,QProgressBar)
import pyqtgraph as pg
from gui_app import LockinWindow,engineering,status_description
from gui_widgets import InstrumentTabBar
from .data import Parameters
from .histogram import Histogram
from .worker import MeasurementWorker


def field(value,low,high,decimals=3):
    widget=QDoubleSpinBox();widget.setDecimals(decimals);widget.setRange(low,high)
    widget.setValue(value);widget.setKeyboardTracking(False);return widget


class MeasurementWindow(LockinWindow):
    worker_class=MeasurementWorker

    def __init__(self,args):
        self.cycle_state='idle';self.start_payload=None;self.stop_event=None
        self.close_requested=False;self.last_result=None;self.count=0;self.run_start=None
        super().__init__(args)
        controls=self.takeCentralWidget()
        self.pages=QTabWidget();self.pages.setTabBar(InstrumentTabBar(self.pages))
        self.setCentralWidget(self.pages)
        page=QWidget();layout=QVBoxLayout(page)
        title=QLabel('SIGNAL RECOVERY 7230 · MAGNITUDE / PHASE')
        title.setStyleSheet('font-size:20px; font-weight:600; color:#164768; padding:8px;');layout.addWidget(title)
        connection=QHBoxLayout();self.cycle_connect=QPushButton('Подключить USB')
        self.cycle_connect.clicked.connect(lambda:self.submit('open'))
        connection.addWidget(self.cycle_connect);self.cycle_connection=QLabel('USB не подключён');connection.addWidget(self.cycle_connection,1)
        settings_button=QPushButton('Настройки Lock-in →');connection.addWidget(settings_button)
        settings_button.clicked.connect(lambda:self.pages.setCurrentIndex(1));layout.addLayout(connection)
        readouts=QHBoxLayout();self.live_mag=QLabel('—');self.live_phase=QLabel('—')
        for name,label in (('Magn1',self.live_mag),('Phase1',self.live_phase)):
            card=QGroupBox(name);card.setObjectName('measurementCard');box=QVBoxLayout(card)
            label.setStyleSheet('font-size:32px;font-weight:600;color:#123c60;');label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            box.addWidget(label);readouts.addWidget(card)
        layout.addLayout(readouts)
        split=QSplitter(Qt.Horizontal);layout.addWidget(split,1)
        left=QWidget();left_layout=QVBoxLayout(left)
        self.parameters_group=QGroupBox('Параметры цикла');form=QFormLayout(self.parameters_group)
        self.sig=field(0,-1e9,1e9,6);self.bal=field(0,-1e9,1e9,6)
        self.acquisition_time=field(10,.01,604800);self.delay=field(100,1,3600000)
        self.e_field=QLineEdit();self.h_field=QLineEdit()
        self.e_field.setPlaceholderText('Значение и единица, например 2.1 kV/cm')
        self.h_field.setPlaceholderText('Значение и единица, например 53.2 mT')
        self.additional=QPlainTextEdit();self.additional.setPlaceholderText('Образец, температура, условия эксперимента…');self.additional.setMaximumHeight(140)
        for name,widget in (('Sig, mV',self.sig),('Bal, mV',self.bal),('Acquisition time, s',self.acquisition_time),
            ('E field',self.e_field),('H field',self.h_field),('Delay, ms',self.delay),('Additional',self.additional)):
            form.addRow(name,widget)
        note=QLabel('Sig, Bal, E field и H field записываются в метаданные.\nMagn1 сохраняется в V (или A в токовом режиме), Phase1 — в градусах.')
        note.setWordWrap(True);form.addRow(note);left_layout.addWidget(self.parameters_group)
        self.output_group=QGroupBox('Сохранение');form=QFormLayout(self.output_group)
        default=Path(QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) or str(Path.home()))/'LockinMeasurements'
        self.folder=QLineEdit(str(default));self.browse=QPushButton('Папка…')
        row=QHBoxLayout();row.addWidget(self.folder,1);row.addWidget(self.browse);form.addRow('Папка данных',row)
        self.browse.clicked.connect(self.choose_folder);left_layout.addWidget(self.output_group)
        row=QHBoxLayout();self.cycle_start_button=QPushButton('▶ Start');self.cycle_stop_button=QPushButton('■ Stop')
        self.cycle_start_button.setStyleSheet('background:#126e96;color:white;font-weight:600;padding:10px;')
        self.cycle_stop_button.setStyleSheet('color:#b42318;padding:10px;')
        row.addWidget(self.cycle_start_button);row.addWidget(self.cycle_stop_button);left_layout.addLayout(row)
        self.retry_button=QPushButton('Сохранить повторно в выбранную папку');self.retry_button.hide();left_layout.addWidget(self.retry_button)
        self.progress=QProgressBar();self.progress.setRange(0,1000);left_layout.addWidget(self.progress)
        self.cycle_status=QLabel('Ожидание · запись не ведётся');self.cycle_status.setWordWrap(True);left_layout.addWidget(self.cycle_status)
        self.paths=QLabel();self.paths.setWordWrap(True);self.paths.setTextInteractionFlags(Qt.TextSelectableByMouse);left_layout.addWidget(self.paths)
        self.note=QLabel('Delay задаёт запрошенный период начала чтений. Если USB медленнее, пропущенные интервалы не накапливаются.\nMagn1 и Phase1 читаются последовательно.')
        self.note.setWordWrap(True);left_layout.addWidget(self.note);left_layout.addStretch()
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(left);scroll.setMinimumWidth(400);split.addWidget(scroll)
        charts=QWidget();chart_layout=QVBoxLayout(charts);self.plots=[];self.bars=[]
        for title,color in (('Распределение Magn1','#197d9c'),('Распределение Phase1','#8b5fa8')):
            plot=pg.PlotWidget(background='w');plot.setTitle(title,color='#25435d',size='13pt')
            plot.setLabel('left','Число отсчётов');plot.showGrid(x=True,y=True,alpha=.12)
            bar=pg.BarGraphItem(x0=[0],x1=[1],height=[0],brush=color,pen=None);plot.addItem(bar)
            chart_layout.addWidget(plot,1);self.plots.append(plot);self.bars.append(bar)
        self.plots[0].setLabel('bottom','Magn1',units='V')
        self.plots[1].setLabel('bottom','Phase1',units='deg');self.plots[1].getAxis('bottom').enableAutoSIPrefix(False)
        split.addWidget(charts);split.setSizes([440,900])
        self.histograms=[Histogram(),Histogram()]
        self.pages.addTab(page,'Измерительный цикл');self.pages.addTab(controls,'Управление Lock-in')
        self.setWindowTitle('Signal Recovery 7230 · Histogram · '+('DEMO' if args.demo else 'USB'))
        self.resize(1540,1000)
        self.cycle_start_button.clicked.connect(self.start_cycle);self.cycle_stop_button.clicked.connect(self.stop_cycle)
        self.retry_button.clicked.connect(self.retry_save)
        self.worker.cycle_started.connect(self.cycle_started)
        self.worker.cycle_samples.connect(self.cycle_samples)
        self.worker.cycle_finished.connect(self.cycle_finished)
        self.worker.cycle_saving.connect(self.cycle_saving)
        self.hist_timer=QTimer(self);self.hist_timer.setInterval(200);self.hist_timer.timeout.connect(self.draw_histograms)
        self.update_enabled()

    def choose_folder(self):
        path=QFileDialog.getExistingDirectory(self,'Папка .dat и .txt',self.folder.text())
        if path:self.folder.setText(path)

    def update_enabled(self):
        super().update_enabled()
        active=self.cycle_state in ('draining','preparing','running','stopping','saving')
        if active:
            self.tabs.setEnabled(False)
            for widget in (self.connect_button,self.disconnect_button,self.refresh_button,self.legacy,self.auto_poll):widget.setEnabled(False)
        else:
            self.legacy.setEnabled(not self.closing);self.auto_poll.setEnabled(not self.closing)
        if not hasattr(self,'parameters_group'):return
        self.cycle_start_button.setEnabled(self.connected and self.cycle_state=='idle' and not self.closing)
        self.cycle_stop_button.setEnabled(active and self.cycle_state not in ('stopping','saving'))
        self.parameters_group.setEnabled(not active and self.cycle_state!='unsaved')
        self.output_group.setEnabled(not active)
        self.retry_button.setVisible(self.cycle_state=='unsaved');self.retry_button.setEnabled(self.cycle_state=='unsaved')
        self.cycle_connect.setEnabled(not self.connected and not active and not self.busy and not self.closing)
        self.cycle_connection.setText(self.connection_label.text())
        if not self.connected:self.live_mag.setText('—');self.live_phase.setText('—')
        if self.closing:self.cycle_start_button.setEnabled(False)

    def schedule_poll(self,*args):
        if self.cycle_state in ('draining','preparing','running','stopping','saving'):self.poll_timer.stop()
        else:super().schedule_poll(*args)

    def poll(self):
        selections=[(s.currentIndex(),u.isChecked()) for s,u,_ in self.reading_controls]+[(2,True),(3,True)]
        self.submit('poll',dict(selections=selections,rear=self.tabs.currentIndex()==2,oscillator=self.tabs.currentIndex()==1))

    def submit(self,job,payload=None):
        if self.cycle_state in ('draining','preparing','running','stopping','saving'):return False
        return super().submit(job,payload)

    def dispatch_next(self):
        if self.cycle_state in ('preparing','running','stopping','saving'):return
        super().dispatch_next()
        if (self.cycle_state=='draining' and not self.busy and not self.pending_jobs
            and not self.pending_edits and not self.edit_timer.isActive()):
            if not self.connected:
                self.cycle_state='idle';self.update_enabled();return
            self.cycle_state='preparing';self.cycle_status.setText('Подготовка · чтение текущих настроек прибора…')
            self.update_enabled();super().start_job('cycle_start',self.start_payload)

    def start_cycle(self):
        if self.cycle_state!='idle' or not self.connected:return
        try:
            parameters=Parameters(self.sig.value(),self.bal.value(),self.acquisition_time.value(),self.delay.value(),
                self.e_field.text(),self.h_field.text(),self.additional.toPlainText())
            parameters.validate()
            if not self.folder.text().strip():raise ValueError('Выберите папку для сохранения.')
            folder=str(Path(self.folder.text()).expanduser().resolve())
            if Path(folder).exists() and not Path(folder).is_dir():raise ValueError('Путь сохранения должен указывать на папку.')
        except Exception as exc:self.cycle_status.setText(str(exc));return
        self.stop_event=Event();self.start_payload=dict(parameters=parameters,folder=folder,event=self.stop_event)
        self.cycle_state='draining';self.count=0;self.run_start=None;self.last_result=None
        self.histograms=[Histogram(),Histogram()];self.paths.clear();self.progress.setValue(0)
        for bar in self.bars:bar.setOpts(x0=[0],x1=[1],height=[0])
        self.cycle_status.setText('Ожидание завершения текущих USB-команд…')
        self.poll_timer.stop();self.update_enabled();self.dispatch_next()

    def stop_cycle(self):
        if self.stop_event:self.stop_event.set()
        if self.cycle_state=='draining':
            self.cycle_state='idle';self.cycle_status.setText('Старт отменён · измерения ещё не начались')
            self.update_enabled();self.dispatch_next()
            if self.close_requested:self.close()
            return
        if self.cycle_state in ('preparing','running'):
            self.cycle_state='stopping';self.cycle_status.setText('Остановка · ожидается завершение текущего USB-чтения и сохранение…')
            self.request.emit('cycle_stop',{});self.update_enabled()

    @pyqtSlot(object)
    def cycle_started(self,metadata):
        if self.cycle_state!='stopping':self.cycle_state='running'
        self.run_start=time.monotonic();self.magnitude_unit=metadata['lockin']['magnitude_unit']
        self.plots[0].setLabel('bottom','Magn1',units=self.magnitude_unit)
        self.hist_timer.start();self.update_enabled()

    @pyqtSlot(object)
    def cycle_samples(self,rows):
        self.count+=len(rows)
        self.histograms[0].add(r[2] for r in rows);self.histograms[1].add(r[3] for r in rows)
        if rows:
            last=rows[-1];self.live_mag.setText(engineering(last[2],getattr(self,'magnitude_unit','V')))
            self.live_phase.setText(f'{last[3]:.8g} deg')
            for selector,units,label in self.reading_controls:
                if selector.currentIndex()==2 and units.isChecked():label.setText(self.live_mag.text())
                elif selector.currentIndex()==3:label.setText(self.live_phase.text())
            self.live_label.setText(status_description(last[4],last[5]))

    def draw_histograms(self):
        for histogram,bar in zip(self.histograms,self.bars):
            edges,counts=histogram.update()
            if edges is not None:bar.setOpts(x0=edges[:-1],x1=edges[1:],height=counts)
        if self.cycle_state=='running' and self.run_start is not None:
            elapsed=time.monotonic()-self.run_start;duration=self.start_payload['parameters'].acquisition_time_s
            self.progress.setValue(min(1000,int(elapsed/duration*1000)))
            self.cycle_status.setText(f'Измерение · {elapsed:.1f} / {duration:g} s · {self.count} точек')

    @pyqtSlot(object)
    def cycle_finished(self,result):
        self.hist_timer.stop();self.draw_histograms();self.last_result=result
        self.cycle_state='idle' if result['ok'] else 'unsaved'
        metadata=result['metadata'];self.progress.setValue(1000 if metadata['result']=='complete' else self.progress.value())
        if result['ok']:
            for histogram in self.histograms:histogram.values.clear()
            self.paths.setText('DAT: '+result['paths']['data']+'\nTXT: '+result['paths']['metadata'])
            self.cycle_status.setText(f'Сохранено · {metadata["sample_count"]} точек · завершение: {metadata["result"]}')
        else:
            self.cycle_status.setText('Ошибка сохранения: '+result['error']+'\nДанные остаются в памяти. Выберите другую папку и нажмите «Сохранить повторно».')
            self.close_requested=False
        self.update_enabled();self.dispatch_next()
        if self.close_requested and result['ok']:QTimer.singleShot(0,self.close)

    @pyqtSlot()
    def cycle_saving(self):
        self.hist_timer.stop();self.draw_histograms()
        self.cycle_state='saving';self.cycle_status.setText(f'Сохранение {self.count} точек в DAT и TXT…')
        self.update_enabled()

    def retry_save(self):
        if self.cycle_state!='unsaved':return
        self.cycle_state='saving';self.update_enabled();self.cycle_status.setText('Повторное сохранение…')
        self.request.emit('cycle_save',dict(folder=str(Path(self.folder.text()).expanduser().resolve())))

    @pyqtSlot(str,object)
    def on_result(self,job,data):
        if job=='cycle_start':
            self.busy=False;self.inflight=None;self.update_enabled();self.dispatch_next();return
        if job=='poll' and hasattr(self,'live_mag'):
            self.live_mag.setText(data['readings'][-2]);self.live_phase.setText(data['readings'][-1])
        rejected=job=='apply' and any(state.get('error') for state in data.values())
        if rejected and self.cycle_state=='draining':
            self.cycle_state='idle';self.cycle_status.setText('Старт отменён: прибор отклонил изменение настройки. Проверьте журнал Lock-in.')
        super().on_result(job,data)

    @pyqtSlot(str,str,bool)
    def on_error(self,job,message,fatal):
        super().on_error(job,message,fatal)
        if job=='cycle_start' or (fatal and self.cycle_state=='draining'):
            self.cycle_state='idle';self.cycle_status.setText('Запуск не выполнен: '+message);self.update_enabled()

    def closeEvent(self,event):
        if self.cycle_state in ('draining','preparing','running','stopping','saving'):
            event.ignore();self.close_requested=True;self.stop_cycle();return
        if self.cycle_state=='unsaved':
            event.ignore();self.cycle_status.setText('Перед закрытием сохраните накопленные данные. Выберите доступную папку и нажмите «Сохранить повторно».');return
        if hasattr(self,'hist_timer'):self.hist_timer.stop()
        super().closeEvent(event)
