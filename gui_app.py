#!/usr/bin/env python3
"""Signal Recovery 7230 / PyQt5. Python 3.9+.

USB and instrument logic: USB7230, Instrument, Worker.
Widgets and validation: Field, Section, LockinWindow.
Run: python gui_app.py [--demo] [--serial 14078415] [--max-frequency 250000]
"""
import argparse
import importlib
import math
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from PyQt5.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPlainTextEdit,
    QPushButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)


class CommandError(Exception):
    """Instrument rejected a command; USB connection remains usable."""
    def __init__(self, command, status, overload=0):
        self.command, self.status, self.overload = command, status, overload
        reason = 'неизвестная команда' if status & 2 else 'недопустимый параметр'
        super().__init__(f'{command}: {reason} (ST=0x{status:02X})')


class ProtocolError(Exception):
    pass


def parse_reply(raw, status_expected=True):
    """Decode ASCII payload separately from the binary USB trailer (§6.4.03)."""
    raw = bytes(raw)
    pos = raw.find(b'\x00')
    if pos < 0:
        raise ProtocolError(f'Ответ без NUL: {raw[:100]!r}')
    trailer = raw[pos + 1:]
    if len(trailer) not in (0, 2) or (status_expected and len(trailer) != 2):
        raise ProtocolError(f'Неверный USB-терминатор: {trailer!r}')
    return (raw[:pos].decode('ascii').strip(),
            trailer[0] if trailer else 0, trailer[1] if trailer else 0)


class USB7230:
    """Only this class handles VISA. Construct and use it in the worker thread."""
    def __init__(self, serial=None, timeout=2000):
        self.module = None
        self.inst = None
        self.serial, self.timeout = serial, timeout

    def open(self):
        # The supplied module creates ResourceManager at import time.
        # Lazy import keeps VISA initialization out of the GUI thread.
        self.module = importlib.import_module('lockin_7230_usb')
        if self.serial:
            self.module._USB_RESOURCE = f'USB0::0x0A2D::0x0027::{self.serial}::RAW'
        self.inst = self.module.Connection_Open_USB()
        self.inst.timeout = self.timeout
        self.inst.read_termination = None
        self.inst.write_termination = None
        # Unknown initial USBTERM: read the query with either trailer, then enable it.
        self.exchange('USBTERM', status_expected=False)
        self.exchange('USBTERM 1', status_expected=False)
        self.exchange('USBTERM')  # From now on require the binary status trailer.

    def exchange(self, command, status_expected=True):
        if not self.inst:
            raise RuntimeError('USB не подключён')
        self.inst.write_raw(command.encode('ascii') + b'\x00')
        # Always drain the response, even for a write-only command.
        reply = parse_reply(self.inst.read_raw(), status_expected)
        if reply[1] & 6:
            raise CommandError(command, reply[1], reply[2])
        return reply

    def close(self):
        inst, self.inst = self.inst, None
        if inst is not None:
            self.module.Connection_Close(inst)


@dataclass
class Field:
    key: str
    label: str
    command: str
    low: float = 0
    high: float = 1
    decimals: int = 0
    scale: float = 1
    choices: list = field(default_factory=list)
    index: Optional[int] = None
    channel: int = 0
    writable: bool = True
    legacy: bool = False

    def cmd(self, mode):
        suffix = str(self.channel) if mode and self.channel else ''
        return self.command.replace('{ch}', suffix)

    def active(self, mode):
        return not (self.channel == 2 and mode == 0)


def choice(key, label, command, labels, **kwargs):
    options = [(x, i) if isinstance(x, str) else x for i, x in enumerate(labels)]
    return Field(key, label, command, choices=options, **kwargs)


def number(key, label, command, low, high, decimals=0, scale=1, **kwargs):
    return Field(key, label, command, low, high, decimals, scale, **kwargs)


def schema(max_frequency):
    """Control values are command codes, never inferred from combo-box indices."""
    tc = ['10 µs', '20 µs', '50 µs', '100 µs', '200 µs', '500 µs',
          '1 ms', '2 ms', '5 ms', '10 ms', '20 ms', '50 ms', '100 ms',
          '200 ms', '500 ms', '1 s', '2 s', '5 s', '10 s', '20 s',
          '50 s', '100 s', '200 s', '500 s', '1 ks']
    sources = ['Internal', 'External digital', 'External analog']
    sections = {
        'Input': [choice('input', 'Input mode', 'INPUT',
                         ['Off', 'A (voltage)', '-B (voltage)', 'A−B (voltage)',
                          'B current, 10⁶ V/A', 'B current, 10⁸ V/A']),
                  choice('acgain', 'AC gain', 'ACGAIN', [f'{6*i} dB' for i in range(16)]),
                  choice('automatic', 'Automatic AC gain', 'AUTOMATIC', ['Off', 'On']),
                  choice('couple', 'Input coupling', 'DCCOUPLE', ['AC', 'DC']),
                  choice('fet', 'Input device', 'FET', ['Bipolar', 'FET']),
                  choice('float', 'Input shield', 'FLOAT', ['Ground', 'Float']),
                  choice('lf', 'Line filter', 'LF', [('Off', 0), ('50 Hz', 5),
                      ('100 Hz', 6), ('50 + 100 Hz', 7), ('60 Hz', 1),
                      ('120 Hz', 2), ('60 + 120 Hz', 3)])],
        'Demodulator': [choice('mode', 'Mode', 'REFMODE',
                       ['Single reference', 'Dual harmonic', 'Dual reference']),
                       choice('demod2', 'Demod2 source', 'DEMOD2SRC',
                              ['Signal channel ADC', 'Rear ADC1', 'Demod1 output'])],
        'Output filters / Noise': [choice('fast', 'Fast mode', 'FASTMODE', ['Off', 'On']),
                       choice('sync', 'Synchronous', 'SYNC', ['Off', 'On', 'Auto (web firmware)'], legacy=True),
                       choice('noise', 'Noise measurement', 'NOISEMODE', ['Off', 'On']),
                       choice('nnbuf', 'Noise buffer', 'NNBUF', ['Off', '1 s', '2 s', '3 s', '4 s'])],
        'Oscillator output': [number('oa', 'Amplitude, V rms', 'OA.', 0, 5, 6),
                       number('of', 'Frequency, Hz', 'OF.', 0, max_frequency, 3)],
        'Frequency / Amplitude Sweep': [
                       number('fstart', 'Start frequency, Hz', 'FSTART.', 0, max_frequency, 3),
                       number('fstop', 'Stop frequency, Hz', 'FSTOP.', 0, max_frequency, 3),
                       choice('fkind', 'Frequency step mode', 'FSTEP.',
                              ['Logarithmic (%)', 'Linear (Hz)', 'Linear seek (Hz)'], index=1),
                       number('fstep', 'Frequency step, Hz or %', 'FSTEP.', 0, max_frequency, 3, index=0),
                       number('astart', 'Start amplitude, V rms', 'ASTART.', 0, 5, 6),
                       number('astop', 'Stop amplitude, V rms', 'ASTOP.', 0, 5, 6),
                       number('astep', 'Amplitude step, V', 'ASTEP.', 0, 5, 6),
                       number('srate', 'Time per step, s', 'SRATE.', .001, 1000, 3)],
        'Modulation': [choice('menable', 'Enable modulation', 'MENABLE', ['Off', 'AM', 'FM'])],
        'Frequency modulation (VCO)': [
                       number('fmcenterf', 'Center frequency, Hz', 'FMCENTERF.', 0, max_frequency, 3),
                       number('fmspanf', 'Frequency span ±Hz', 'FMSPANF.', 0, max_frequency/2, 3),
                       number('fmcenterv', 'Center voltage, V', 'FMCENTERV.', -10, 10, 3),
                       number('fmvspan', 'Voltage span ±V', 'FMVSPAN.', 0, 10, 3),
                       number('fmfilter', 'Filter control', 'FMFILTER', 0, 10)],
        'Amplitude modulation': [choice('amsource', 'Source', 'AMSOURCE', ['ADC1', 'External reference']),
                       number('amdepth', 'Modulation depth, %', 'AMDEPTH', 0, 100),
                       number('amcenterv', 'Center voltage, V', 'AMCENTERV.', -10, 10, 3),
                       number('amvspan', 'Voltage span ±V', 'AMVSPAN.', 0, 10, 3),
                       number('amfilter', 'Filter control', 'AMFILTER', 0, 10)],
        'Digital Port': [number('portdir', 'Direction mask (1 = input)', 'PORTDIR', 0, 255),
                       number('byte', 'Output mask D7…D0', 'BYTE', 0, 255),
                       number('readbyte', 'Input mask D7…D0', 'READBYTE', 0, 255, writable=False)],
        'ADCs': [choice('tadc', 'ADC trigger', 'TADC', ['Internal (1 kHz)', 'External'])] + [
                       number(f'adc{i}', f'ADC{i}, V', f'ADC. {i}', -11, 11, 3, writable=False)
                       for i in range(1, 5)],
        'RS232': [choice('baud', 'Baud rate, bps', 'RS',
                       ['75', '110', '134.5', '150', '300', '600', '1200', '1800',
                        '2000', '2400', '4800', '9600', '19200', '38400'], index=0),
                 choice('bits', 'Data bits', 'RSBITS', ['7', '8']),
                 choice('parity', 'Parity', 'RSPARITY', ['None', 'Even', 'Odd']),
                 choice('echo', 'Echo', 'RSECHO', ['Off', 'On']),
                 choice('prompt', 'Prompt', 'RSPROMPT', ['Off', 'On']),
                 choice('delimiter', 'Delimiter', 'DD', [('CR', 13), ('space', 32),
                         ('comma', 44), ('colon', 58), ('semicolon', 59)])],
        'Ref monitor / Expand': [choice('refmon', 'Reference on TRIG OUT', 'REFMON', ['Off', 'On']),
                 choice('expand', 'Expand ×10 (single mode)', 'EX', ['Off', 'X1', 'Y1', 'X1 + Y1'])],
    }
    for ch in (1, 2):
        sections[f'Reference / Output {ch}'] = [
            choice(f'ie{ch}', 'Reference source', 'IE{ch}', sources +
                   (['Virtual (seek + tracking)'] if ch == 1 else []), channel=ch),
            number(f'frq{ch}', 'Reference frequency, Hz', 'FRQ{ch}.', 0, max_frequency,
                   3, channel=ch, writable=False),
            number(f'refn{ch}', 'Harmonic', 'REFN{ch}', 1, 127, channel=ch),
            number(f'refp{ch}', 'Phase, degrees', 'REFP{ch}.', -360, 360, 3, channel=ch),
            choice(f'sen{ch}', 'Sensitivity', 'SEN{ch}', [(f'SEN {i}', i) for i in range(3, 28)], channel=ch),
            choice(f'tc{ch}', 'Time constant', 'TC{ch}', tc, channel=ch),
            choice(f'slope{ch}', 'Slope', 'SLOPE{ch}',
                   ['6 dB/oct', '12 dB/oct', '18 dB/oct', '24 dB/oct'], channel=ch),
        ]
        for axis in ('X', 'Y'):
            base = f'{axis}OF'
            sections[f'Reference / Output {ch}'] += [
                choice(f'{base.lower()}{ch}en', f'{axis} offset', base+'{ch}',
                       ['Off', 'On'], index=0, channel=ch),
                number(f'{base.lower()}{ch}val', f'{axis} offset, % FS', base+'{ch}',
                       -300, 300, 2, 100, index=1, channel=ch)]
    common = ['Noise %', 'Ratio (10 × X% / ADC1)', 'Log ratio', 'Equation 1',
              'Equation 2', 'User setting']
    monitors = ['Digital signal monitor', 'ADC1 monitor', 'Digital signal monitor 2', 'Sync oscillator']
    duals = ['Magnitude2 %', 'Phase2', 'X2 %', 'Y2 %']
    sections['DACs'] = []
    for i, name in enumerate(['X1 %', 'Y1 %', 'Magnitude1 %', 'Phase1'], 1):
        sections['DACs'] += [choice(f'ch{i}', f'DAC{i} source', f'CH {i}',
                                    [name] + common + [monitors[i-1], duals[i-1]]),
                            number(f'dac{i}', f'DAC{i} user voltage, V', f'DAC. {i}', -10, 10, 3)]
    # SOA is used by the supplied web page but absent from the attached manual.
    sections['DACs'] += [number('soa', 'Sync oscillator amplitude, V (web SOA)',
                                'SOA', 0, 10, 3, 1000, legacy=True)]
    variables = ['X1', 'Y1', 'MAG1', 'PHA1', 'ADC1', 'ADC2', 'ADC3', 'ADC4',
                 'C1', 'C2', '0', '1', 'FRQ', 'OSCF', 'X2', 'Y2', 'MAG2', 'PHA2']
    for i in (1, 2):
        fields = [number(f'c1{i}', 'Constant C1', f'C1 {i}', -30000, 30000),
                  number(f'c2{i}', 'Constant C2', f'C2 {i}', -30000, 30000)]
        for k, label in enumerate(['A', '+ / −', 'B', 'C', 'D']):
            opts = [('−', 0), ('+', 1)] if k == 1 else [(v, j) for j, v in enumerate(variables)
                                                           if k == 3 or j not in (12, 13)]
            fields.append(choice(f'eq{i}_{k}', label, f'DEFEQU {i}', opts, index=k))
        sections[f'Equation {i}: ((A ± B) × C) / D'] = fields
    return sections


def split_numbers(text):
    # DD is mutable: accept all documented delimiters, including carriage return.
    return [float(v) for v in re.findall(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', text)]


class Instrument:
    """Command mapping and validation are independent of widgets and Qt."""
    def __init__(self, transport, sections, max_frequency):
        self.transport, self.sections = transport, sections
        self.mode, self.max_frequency = 0, max_frequency
        self.status = self.overload = 0
        self.legacy = False
        self.unsupported = set()

    def query(self, cmd):
        text, self.status, self.overload = self.transport.exchange(cmd)
        return text

    def values(self, cmd):
        vals = split_numbers(self.query(cmd))
        if not vals:
            raise ProtocolError(f'{cmd}: ожидался числовой ответ')
        return vals

    def channel_command(self, base, ch):
        return base + (str(ch) if self.mode else '')

    def read_section(self, title):
        self.mode = int(self.values('REFMODE')[0])
        result, cache, warnings = {}, {}, []
        for f in self.sections[title]:
            if not f.active(self.mode) or (f.legacy and not self.legacy):
                continue
            cmd = f.cmd(self.mode)
            if f.key == 'expand' and self.mode:
                continue
            if f.key == 'demod2' and self.mode != 2:
                continue
            if f.key == 'ie2' and self.mode == 1:
                cmd = 'IE'
            if f.key == 'ie1':
                cmd = 'IE1' if self.mode == 2 else 'IE'
            if f.key == 'input':
                imode = int(self.values('IMODE')[0])
                result[f.key] = int(self.values('VMODE')[0]) if imode == 0 else imode + 3
                continue
            if f.key in ('bits', 'parity', 'echo', 'prompt'):
                if 'RS' not in cache:
                    cache['RS'] = self.values('RS')
                rs = int(cache['RS'][1])
                result[f.key] = {'bits': rs & 1, 'parity': 0 if not rs & 2 else
                                (2 if rs & 4 else 1), 'echo': bool(rs & 8), 'prompt': bool(rs & 16)}[f.key]
                continue
            if cmd in self.unsupported:
                warnings.append(cmd+': не поддерживается прибором')
                continue
            try:
                if cmd not in cache:
                    cache[cmd] = self.values(cmd)
                vals = cache[cmd]
                if f.key == 'lf':
                    result[f.key] = int(vals[0]) + 4 * int(vals[1])
                else:
                    result[f.key] = vals[f.index or 0] / f.scale
                    if f.key == 'ie1' and not self.mode and int(self.values('VRLOCK')[0]):
                        result[f.key] = 3
            except CommandError as exc:
                self.unsupported.add(cmd)
                warnings.append(str(exc))
            except IndexError as exc:
                raise ProtocolError(f'{cmd}: неполный ответ {cache.get(cmd)}') from exc
        return {'mode': self.mode, 'values': result, 'warnings': warnings,
                'unsupported': list(self.unsupported)}

    def validate(self, title, v, changed):
        for f in self.sections[title]:
            if f.key not in v or not f.writable or f.key not in changed:
                continue
            value = v[f.key]
            if not math.isfinite(float(value)):
                raise ValueError(f'{f.label}: требуется конечное число')
            if f.choices and value not in [c[1] for c in f.choices]:
                raise ValueError(f'{f.label}: неизвестный код {value}')
            if not f.choices and not f.low <= value <= f.high:
                raise ValueError(f'{f.label}: допустимо {f.low}…{f.high}')
        if title == 'Frequency / Amplitude Sweep':
            if v['fkind'] == 0 and (v['fstep'] > 100 or min(v['fstart'], v['fstop']) <= 0):
                raise ValueError('Log sweep: частоты > 0, шаг ≤ 100 %')
            if v['fstart'] != v['fstop'] and v['fstep'] <= 0:
                raise ValueError('Для частотной развёртки нужен положительный шаг')
            if v['astart'] != v['astop'] and v['astep'] <= 0:
                raise ValueError('Для амплитудной развёртки нужен положительный шаг')
        if title == 'Frequency modulation (VCO)':
            if v['fmspanf'] > min(v['fmcenterf'], self.max_frequency-v['fmcenterf']):
                raise ValueError('Center frequency ± span выходит за диапазон генератора')
        for prefix in ('fm', 'am'):
            if prefix+'centerv' in v and abs(v[prefix+'centerv']) + v[prefix+'vspan'] > 10:
                raise ValueError('Модуль center voltage + voltage span должен быть ≤ 10 V')
        if title.startswith('Reference / Output'):
            ch = int(title[-1])
            if not self.mode and ch == 2:
                raise ValueError('Канал 2 доступен только в двойном режиме')
            imode = int(self.values('IMODE')[0])
            if imode == 2 and v[f'sen{ch}'] < 7:
                raise ValueError('В режиме low noise допустим SEN ≥ 7')
            noise = int(self.values('NOISEMODE')[0])
            fast = int(self.values('FASTMODE')[0])
            tc, slope = v[f'tc{ch}'], v[f'slope{ch}']
            if noise and not 5 <= tc <= 9:
                raise ValueError('Noise mode: TC должен быть 500 µs…10 ms')
            if not fast and tc < 8:
                raise ValueError('Для TC < 5 ms включите Fast mode')
            if (fast or noise) and slope > 1:
                raise ValueError('Fast/Noise mode: допустимы только 6/12 dB/oct')
            if self.mode == 2 and ch == 2 and v['refn2'] != 1:
                raise ValueError('Dual reference: harmonic 2 должен быть равен 1')
            if v.get('ie1') == 3 and self.mode:
                raise ValueError('Virtual требует Single reference')
        if v.get('sync') == 2 and not self.legacy:
            raise ValueError('SYNC 2 есть в HTML, но не описан в PDF; включите web-команды')

    def apply(self, title, v, changed):
        self.mode = int(self.values('REFMODE')[0])
        self.validate(title, v, changed)
        commands, handled = [], set()
        # Compound controls are written together; unchanged values are never re-applied.
        for f in self.sections[title]:
            if f.key not in changed or not f.writable or not f.active(self.mode):
                continue
            if f.legacy and not self.legacy:
                raise ValueError(f'{f.command}: включите web-команды для этой прошивки')
            cmd = f.cmd(self.mode)
            if cmd in handled:
                continue
            handled.add(cmd)
            if f.key == 'input':
                n = int(v['input'])
                commands += [f'IMODE {0 if n < 4 else n-3}']
                if n < 4:
                    commands += [f'VMODE {n}']
            elif cmd == 'LF':
                n = int(v['lf']); commands += [f'LF {n % 4} {n // 4}']
            elif cmd.startswith(('XOF', 'YOF')):
                k = f.command[:3].lower()+str(f.channel)
                commands += [f'{cmd} {int(v[k+"en"])} {round(v[k+"val"]*100)}']
            elif cmd.startswith('DEFEQU'):
                eq = int(cmd[-1])
                codes = [int(v[f'eq{eq}_{k}']) for k in range(5)]
                if not self.mode and any(codes[k] >= 14 for k in (0, 2, 3, 4)):
                    raise ValueError('Переменные канала 2 доступны только в двойном режиме')
                commands += [cmd+' '+' '.join(map(str, codes))]
            elif f.key == 'ie1':
                n = int(v['ie1'])
                if n == 3:
                    commands += ['FASTMODE 0', 'TC 8', 'IE 0']
                else:
                    commands += ([] if self.mode else ['VRLOCK 0'])
                    commands += [f'{"IE1" if self.mode == 2 else "IE"} {n}']
            elif f.key == 'ie2':
                if self.mode == 1:
                    raise ValueError('В Dual harmonic источник общий; измените Reference 1')
                commands += [f'IE2 {int(v["ie2"])}']
            elif cmd in ('RS', 'RSBITS', 'RSPARITY', 'RSECHO', 'RSPROMPT'):
                if 'rs_group' not in handled:
                    handled.add('rs_group')
                    # PDF: bit2=1 -> odd; the HTML uses the reverse encoding.
                    parity = {0: 0, 1: 2, 2: 6}[int(v['parity'])]
                    flags = int(v['bits']) | parity | (int(v['echo']) << 3) | (int(v['prompt']) << 4)
                    commands += [f'RS {int(v["baud"])} {flags}']
            elif cmd == 'FSTEP.':
                commands += [f'FSTEP. {v["fstep"]:.3f} {int(v["fkind"])}']
            elif f.key.startswith('dac'):
                i = int(f.key[-1])
                source = int(v[f'ch{i}'])
                if source != 6:
                    raise ValueError(f'DAC{i}: выберите User setting для изменения напряжения')
                commands += [f'{cmd} {v[f.key]:.3f}']
            elif f.key == 'expand' and self.mode:
                raise ValueError('EX недоступен в двойном режиме (§6.6.14)')
            elif f.key == 'demod2' and self.mode != 2:
                raise ValueError('DEMOD2SRC требует Dual reference')
            else:
                val = v[f.key] * f.scale
                arg = str(round(val)) if not f.decimals or f.scale != 1 else f'{val:.{f.decimals}f}'
                commands += [f'{cmd} {arg}']
        # Drop spans before changing centers, as recommended in §6.6.06.
        if title == 'Frequency modulation (VCO)' and changed & {'fmcenterf', 'fmspanf'}:
            commands = [c for c in commands if not c.startswith(('FMCENTERF.', 'FMSPANF.'))]
            commands = ['FMSPANF. 0', f'FMCENTERF. {v["fmcenterf"]:.3f}',
                        f'FMSPANF. {v["fmspanf"]:.3f}'] + commands
        for prefix in ('FM', 'AM'):
            if title == ('Frequency modulation (VCO)' if prefix == 'FM' else 'Amplitude modulation'):
                if changed & {prefix.lower()+'centerv', prefix.lower()+'vspan'}:
                    commands = [c for c in commands if not c.startswith((prefix+'CENTERV.', prefix+'VSPAN.'))]
                    commands = [prefix+'VSPAN. 0', f'{prefix}CENTERV. {v[prefix.lower()+"centerv"]:.3f}',
                                f'{prefix}VSPAN. {v[prefix.lower()+"vspan"]:.3f}'] + commands
        completed = []
        for cmd in commands:
            try:
                self.query(cmd)
                completed.append(cmd)
            except CommandError as exc:
                raise ValueError(f'{exc}. Уже выполнено: {completed or "ничего"}. '
                                 'Параметры будут перечитаны; USB-запись не атомарна.') from exc
        return completed

    def poll(self, selections, rear=False, oscillator=False):
        self.mode = int(self.values('REFMODE')[0])
        results, cache = [], {}
        status = overload = 0
        for code, physical in selections:
            if 4 <= code <= 7 and not self.mode:
                results.append('— (канал 2 выключен)'); continue
            if code == 8 and not int(self.values('NOISEMODE')[0]):
                results.append('— (Noise mode выключен)'); continue
            if code > 8:
                cmd, unit, scale = f'EQU {code-8}', '', 1
            elif code == 8:
                cmd, unit, scale = ('NHZ.', 'V/√Hz' if not int(self.values('IMODE')[0]) else 'A/√Hz', 1) if physical else ('NN', '%', .01)
            else:
                ch, base = (1 if code < 4 else 2), ['X', 'Y', 'MAG', 'PHA'][code % 4]
                phase = base == 'PHA'
                cmd = self.channel_command(base, ch) + ('.' if physical or phase else '')
                unit = 'deg' if phase else ('V' if not int(self.values('IMODE')[0]) else 'A') if physical else '%'
                scale = 1 if physical or phase else .01
                if not self.mode and not physical and base in ('X', 'Y'):
                    ex = int(self.values('EX')[0])
                    if ex & (1 if base == 'X' else 2):
                        scale /= 10
            try:
                if cmd not in cache:
                    cache[cmd] = self.values(cmd)[0]
                results.append(f'{cache[cmd]*scale:.8g} {unit}'.strip())
            except CommandError as exc:
                results.append(str(exc))
            status |= self.status; overload |= self.overload
        freq = [self.values(self.channel_command('FRQ', ch)+'.')[0]
                for ch in range(1, 3 if self.mode else 2)]
        status |= self.status; overload |= self.overload
        # ST preserves all bits, unlike the mask in the supplied low-level helper.
        status |= int(self.values('ST')[0]); overload |= int(self.values('N')[0])
        extras = {}
        if rear:
            extras = {f'adc{i}': self.values(f'ADC. {i}')[0] for i in range(1, 5)}
            extras['readbyte'] = self.values('READBYTE')[0]
        if oscillator and not self.mode:
            extras['sweep'] = int(self.values('SWEEP')[0])
        return {'readings': results, 'frequency': freq, 'mode': self.mode, 'extras': extras,
                'status': status, 'overload': overload,
                'oscillator': [self.values('OA.')[0], self.values('OF.')[0]]}


class DemoTransport:
    """Small deterministic command simulator, not a physical model of a 7230."""
    def __init__(self):
        self.data = {}
        self.opened = False
        self.reset()

    def reset(self):
        self.data = {'USBTERM': '1', 'VER': 'DEMO', 'ID': '7230', 'REFMODE': '0',
                     'IMODE': '0', 'VMODE': '1', 'ACGAIN': '0', 'AUTOMATIC': '0',
                     'DCCOUPLE': '0', 'FET': '0', 'FLOAT': '1', 'LF': '0,0',
                     'DEMOD2SRC': '0', 'IE': '0', 'IE1': '0', 'IE2': '2',
                     'FASTMODE': '0', 'SYNC': '0', 'NOISEMODE': '0', 'NNBUF': '4',
                     'OA.': '0.1', 'OF.': '1000', 'FSTART.': '100', 'FSTOP.': '1000',
                     'FSTEP.': '10,1', 'ASTART.': '.1', 'ASTOP.': '.5', 'ASTEP.': '.01',
                     'SRATE.': '.1', 'MENABLE': '0', 'AMSOURCE': '0', 'AMDEPTH': '10',
                     'FMFILTER': '0', 'AMFILTER': '0', 'FMVSPAN.': '0', 'AMVSPAN.': '0',
                     'FMCENTERV.': '0', 'AMCENTERV.': '0', 'FMCENTERF.': '1000',
                     'FMSPANF.': '0', 'PORTDIR': '0', 'BYTE': '0', 'READBYTE': '0',
                     'TADC': '0', 'RS': '12,1', 'DD': '44', 'REFMON': '0', 'EX': '0',
                     'SOA': '0', 'SWEEP': '0', 'VRLOCK': '0', 'ST': '1', 'N': '0'}
        for suffix in ('', '1', '2'):
            for base, value in [('REFN', '1'), ('REFP', '0'), ('SEN', '18'), ('TC', '9'),
                                ('SLOPE', '1'), ('XOF', '0,0'), ('YOF', '0,0')]:
                self.data[base+suffix+('.' if base == 'REFP' else '')] = value
        for i in range(1, 5):
            self.data[f'CH {i}'] = '6'
            self.data[f'DAC. {i}'] = '0'
        for i in (1, 2):
            self.data[f'DEFEQU {i}'] = '0,1,1,8,11'
            self.data[f'C1 {i}'] = '1'; self.data[f'C2 {i}'] = '0'

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False

    def exchange(self, command):
        if not self.opened:
            raise RuntimeError('Demo transport is closed')
        time.sleep(.002)
        words = command.upper().split()
        name = words[0]
        index_cmd = name in ('C1', 'C2', 'DEFEQU', 'EQU', 'CH', 'DAC.', 'ADC.')
        key = ' '.join(words[:2]) if index_cmd else name
        args = words[2:] if index_cmd else words[1:]
        if name == 'ADF':
            self.reset(); return '', 1, 0
        if name.startswith(('AQN', 'AXO', 'AS')) and name not in self.data:
            return '', 1, 0
        if name == 'VRSEARCH':
            return '', 1, 0
        if args:
            if key not in self.data:
                raise CommandError(command, 3)
            self.data[key] = ','.join(args)
            return '', 1, 0
        if key in self.data:
            return self.data[key], 1, 0
        if name.startswith('FRQ'):
            return self.data['OF.'], 1, 0
        if name == 'ADC.':
            return str(int(words[1])*.001), 1, 0
        if name == 'EQU':
            return str(1000*int(words[1])), 1, 0
        if name in ('NHZ.', 'NN'):
            return '1e-8' if name.endswith('.') else '1', 1, 0
        if re.fullmatch(r'(X|Y|MAG|PHA)[12]?\.?', name):
            value = 30 if name.startswith('PHA') else .001
            return str(value if name.endswith('.') else value*1e6), 1, 0
        raise CommandError(command, 3)


class Worker(QObject):
    result = pyqtSignal(str, object)
    failed = pyqtSignal(str, str, bool)
    progress = pyqtSignal(str)
    stopped = pyqtSignal()

    def __init__(self, demo, serial, timeout, sections, max_frequency):
        super().__init__()
        self.args = demo, serial, timeout, sections, max_frequency
        self.instrument = None

    @pyqtSlot(str, object)
    def execute(self, job, payload):
        try:
            if job == 'open':
                demo, serial, timeout, sections, maximum = self.args
                transport = DemoTransport() if demo else USB7230(serial, timeout)
                self.instrument = Instrument(transport, sections, maximum)
                transport.open()
                version = self.instrument.query('VER')
                self.result.emit(job, {'version': version})
                return
            if job == 'close':
                if self.instrument:
                    self.instrument.transport.close()
                self.instrument = None
                self.result.emit(job, {})
                return
            inst = self.instrument
            if inst is None:
                raise ValueError('Нет соединения с прибором')
            inst.legacy = payload.get('legacy', False)
            if job == 'read':
                data = {}
                for title in payload['titles']:
                    self.progress.emit('Чтение: '+title)
                    data[title] = inst.read_section(title)
                self.result.emit(job, data)
            elif job == 'apply':
                title = payload['title']
                completed = inst.apply(title, payload['values'], set(payload['changed']))
                data = inst.read_section(title)
                data['completed'] = completed
                self.result.emit(job, {title: data})
            elif job == 'poll':
                self.result.emit(job, inst.poll(payload['selections'], payload.get('rear', False), payload.get('oscillator', False)))
            elif job == 'raw':
                cmd = payload['command'].strip()
                if not cmd or any(c in cmd for c in '\x00\r\n'):
                    raise ValueError('Введите одну ASCII-команду без NUL и перевода строки')
                cmd.encode('ascii')
                # Bulk transfers require a different receive protocol, not this console.
                if cmd.split()[0].upper().rstrip('.') in ('DC', 'DCB', 'DCT'):
                    raise ValueError('Команды выгрузки буфера не поддерживаются этой консолью')
                words = cmd.upper().split()
                if (words[0] == 'USBTERM' and len(words) > 1 and words[1] != '1') or words == ['ADF', '0']:
                    raise ValueError('Эта команда меняет протокол связи; используйте USBTERM 1 или ADF 1')
                try:
                    answer = inst.query(cmd)
                    self.result.emit(job, (cmd, answer, inst.status, inst.overload))
                except CommandError as exc:
                    self.result.emit(job, (cmd, str(exc), exc.status, exc.overload))
            elif job == 'action':
                action, ch = payload['action'], payload.get('channel', 1)
                inst.mode = int(inst.values('REFMODE')[0])
                if ch == 2 and inst.mode == 0:
                    raise ValueError('Канал 2 выключен')
                if action == 'plus90':
                    cmd = inst.channel_command('REFP', ch)+'.'
                    phase = (inst.values(cmd)[0]+90) % 360
                    inst.query(f'{cmd} {phase:.3f}')
                elif action in ('AS', 'AQN', 'AXO'):
                    inst.query(inst.channel_command(action, ch))
                elif action == 'sweep':
                    if inst.mode:
                        raise ValueError('SWEEP не описан для двойного режима (§6.6.14)')
                    if payload['mask'] == 0:
                        raise ValueError('Отметьте Armed для частоты и/или амплитуды')
                    inst.query(f'SWEEP {payload["mask"]}')
                elif action == 'stop':
                    if inst.mode:
                        raise ValueError('SWEEP требует Single reference (§6.6.14)')
                    inst.query('SWEEP 0')
                elif action == 'search':
                    if inst.mode:
                        raise ValueError('Virtual search требует Single reference')
                    inst.query('VRLOCK 0')
                    if inst.legacy:
                        inst.query('VRSEARCH')
                    else:
                        step = inst.values('FSTEP.')[0]
                        if step <= 0:
                            raise ValueError('Сначала задайте положительный шаг поиска в Oscillator')
                        for cmd in ['IE 0', 'FASTMODE 0', 'TC 8', f'FSTEP. {step:.3f} 2', 'SWEEP 1']:
                            inst.query(cmd)
                elif action == 'lock':
                    if inst.mode:
                        raise ValueError('Virtual tracking требует Single reference')
                    inst.query('VRLOCK 1')
                elif action in ('ADF 1', 'ASM'):
                    inst.query(action)
                    if action == 'ADF 1':
                        inst.query('USBTERM 1')
                self.result.emit(job, {'action': action})
        except (ValueError, CommandError, UnicodeError) as exc:
            fatal = job == 'open'
            if fatal and self.instrument:
                try:
                    self.instrument.transport.close()
                except Exception:
                    pass
                self.instrument = None
            self.failed.emit(job, str(exc), fatal)
        except Exception as exc:
            # A timeout leaves response framing uncertain. Close and explicitly reconnect.
            if self.instrument:
                try:
                    self.instrument.transport.close()
                except Exception:
                    pass
            self.instrument = None
            self.failed.emit(job, f'{type(exc).__name__}: {exc}', True)

    @pyqtSlot()
    def shutdown(self):
        try:
            if self.instrument:
                self.instrument.transport.close()
        except Exception as exc:
            self.failed.emit('shutdown', str(exc), True)
        finally:
            self.instrument = None
            self.stopped.emit()


class Section(QGroupBox):
    apply_requested = pyqtSignal(str)

    def __init__(self, title, fields):
        super().__init__(title)
        self.fields, self.widgets, self.baseline = fields, {}, {}
        self.available = set()
        self.title = title
        grid = QGridLayout(self)
        for row, f in enumerate(fields):
            w = QComboBox() if f.choices or f.key.startswith('sen') else QDoubleSpinBox()
            if isinstance(w, QComboBox):
                for label, value in f.choices:
                    w.addItem(label, value)
            else:
                w.setDecimals(f.decimals); w.setRange(f.low, f.high)
                w.setSingleStep(10 ** (-f.decimals) if f.decimals else 1)
                w.setKeyboardTracking(False)
            w.setMinimumWidth(200)
            w.setToolTip(f.command + (' — web firmware, не описано в PDF' if f.legacy else ''))
            self.widgets[f.key] = w
            grid.addWidget(QLabel(f.label), row, 0); grid.addWidget(w, row, 1)
        self.apply_button = QPushButton('Применить')
        self.apply_button.clicked.connect(lambda: self.apply_requested.emit(title))
        grid.addWidget(self.apply_button, len(fields), 1)
        self.note = QLabel('Параметры ещё не прочитаны')
        self.note.setWordWrap(True)
        grid.addWidget(self.note, len(fields)+1, 0, 1, 2)

    def values(self):
        return {k: w.currentData() if isinstance(w, QComboBox) else w.value()
                for k, w in self.widgets.items()}

    def changed(self):
        return [k for k, v in self.values().items() if k in self.available and
                k in self.baseline and v != self.baseline[k]]

    def load(self, data):
        self.available = set(data['values'])
        for f in self.fields:
            if f.key not in self.available:
                continue
            value, w = data['values'][f.key], self.widgets[f.key]
            if isinstance(w, QComboBox):
                index = w.findData(int(value))
                if index < 0:
                    w.addItem(f'Код {int(value)} (прибор)', int(value)); index = w.count()-1
                w.setCurrentIndex(index)
            else:
                # Preserve valid firmware values outside the supplied page's old ranges.
                w.setRange(min(f.low, value), max(f.high, value)); w.setValue(value)
        self.baseline = self.values()
        self.note.setText('\n'.join(data['warnings']) or 'Прочитано с прибора')

    def enable_fields(self, connected, mode, legacy):
        for f in self.fields:
            enabled = (connected and f.writable and f.active(mode) and f.key in self.available
                       and (not f.legacy or legacy))
            if f.key == 'expand' and mode:
                enabled = False
            if f.key == 'demod2' and mode != 2:
                enabled = False
            if f.key == 'ie2' and mode == 1:
                enabled = False
            self.widgets[f.key].setEnabled(enabled)
        self.apply_button.setEnabled(connected and bool(self.available))


class LockinWindow(QMainWindow):
    request = pyqtSignal(str, object)
    stop_worker = pyqtSignal()

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.sections_data = schema(args.max_frequency)
        self.sections = {}
        self.connected = self.busy = self.closing = self.thread_stopped = False
        self.mode = 0
        self.page_loaded = set()
        self.pending_refresh = False
        self.active_title = None
        self.setWindowTitle('Signal Recovery 7230 — '+('DEMO' if args.demo else 'USB'))
        self.resize(1150, 900)
        root = QWidget(); self.setCentralWidget(root); layout = QVBoxLayout(root)
        bar = QHBoxLayout(); layout.addLayout(bar)
        self.connect_button = QPushButton('Подключиться')
        self.disconnect_button = QPushButton('Отключиться')
        self.refresh_button = QPushButton('Перечитать вкладку')
        self.connect_button.clicked.connect(lambda: self.submit('open'))
        self.disconnect_button.clicked.connect(lambda: self.submit('close'))
        self.refresh_button.clicked.connect(self.refresh)
        for w in (self.connect_button, self.disconnect_button, self.refresh_button):
            bar.addWidget(w)
        self.auto_poll = QCheckBox('Опрос'); self.auto_poll.setChecked(True)
        self.interval = QSpinBox(); self.interval.setRange(250, 60000); self.interval.setValue(1000)
        bar.addWidget(self.auto_poll); bar.addWidget(QLabel('Интервал, мс')); bar.addWidget(self.interval)
        self.legacy = QCheckBox('Web-команды прошивки (не гарантированы PDF)')
        self.legacy.setToolTip('Разрешить SYNC 2, VRSEARCH и SOA из HTML; поддержка USB не гарантирована PDF.')
        self.legacy.toggled.connect(self.legacy_changed); layout.addWidget(self.legacy)
        self.connection_label = QLabel('Не подключено')
        layout.addWidget(self.connection_label)
        self.reading_controls = []
        read_grid = QGridLayout(); layout.addLayout(read_grid)
        labels = ['X1', 'Y1', 'Magnitude1', 'Phase1', 'X2', 'Y2', 'Magnitude2', 'Phase2', 'Noise1', 'Equation1', 'Equation2']
        for col, default in enumerate((2, 3, 6, 7)):
            selector, physical, value = QComboBox(), QCheckBox('Units'), QLabel('—')
            selector.addItems(labels); selector.setCurrentIndex(default); physical.setChecked(True)
            value.setStyleSheet('font-size: 18px; font-weight: bold;')
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            read_grid.addWidget(selector, 0, col); read_grid.addWidget(physical, 1, col)
            read_grid.addWidget(value, 2, col)
            self.reading_controls.append((selector, physical, value))
        self.live_label = QLabel('ST: — | Overload: — | Reference: —')
        self.live_label.setWordWrap(True); layout.addWidget(self.live_label)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs)
        self.page_titles = [
            ['Input', 'Demodulator', 'Reference / Output 1', 'Reference / Output 2', 'Output filters / Noise', 'Oscillator output'],
            ['Oscillator output', 'Frequency / Amplitude Sweep', 'Modulation', 'Frequency modulation (VCO)', 'Amplitude modulation'],
            ['Digital Port', 'ADCs', 'DACs', 'RS232', 'Ref monitor / Expand'],
            ['Equation 1: ((A ± B) × C) / D', 'Equation 2: ((A ± B) × C) / D'],
        ]
        self.page_sections = []
        for page, name in enumerate(['Main Controls', 'Oscillator', 'Rear Panel', 'Equations']):
            content, scroll = QWidget(), QScrollArea()
            scroll.setWidgetResizable(True); scroll.setWidget(content)
            grid = QGridLayout(content); boxes = []
            for i, title in enumerate(self.page_titles[page]):
                box = Section(title, self.sections_data[title]); boxes.append(box)
                self.sections.setdefault(title, []).append(box)
                box.apply_requested.connect(self.apply_section)
                grid.addWidget(box, i//2, i%2, alignment=Qt.AlignTop)
                if title.startswith('Reference / Output'):
                    ch = int(title[-1]); row = QHBoxLayout()
                    for text, action in [('+90°', 'plus90'), ('Auto Phase', 'AQN'), ('Auto Sen', 'AS'), ('Auto Offset', 'AXO')]:
                        b = QPushButton(text); row.addWidget(b)
                        b.clicked.connect(lambda checked=False, a=action, c=ch: self.action(a, c))
                    box.layout().addLayout(row, len(box.fields)+2, 0, 1, 2)
            self.page_sections.append(boxes)
            extra = QGroupBox(); el = QVBoxLayout(extra)
            grid.addWidget(extra, (len(boxes)+1)//2, 0, 1, 2)
            if page == 0:
                el.addWidget(QLabel('Virtual ref: задайте Start/Stop/Step/Time во вкладке Oscillator.'))
                for text, action in [('Search (linear seek; VRSEARCH при включённых web-командах)', 'search'), ('Включить tracking после успешного поиска (VRLOCK 1)', 'lock')]:
                    b = QPushButton(text); el.addWidget(b); b.clicked.connect(lambda checked=False, a=action: self.action(a))
                self.input_info = QLabel('Input limit / dynamic reserve: —'); el.addWidget(self.input_info)
            elif page == 1:
                row = QHBoxLayout(); el.addLayout(row)
                self.arm_frequency = QCheckBox('Frequency armed'); self.arm_amplitude = QCheckBox('Amplitude armed')
                row.addWidget(self.arm_frequency); row.addWidget(self.arm_amplitude)
                self.start_button = QPushButton('Start sweep'); self.stop_button = QPushButton('Stop sweep')
                row.addWidget(self.start_button); row.addWidget(self.stop_button)
                self.start_button.clicked.connect(self.start_sweep); self.stop_button.clicked.connect(lambda: self.action('stop'))
                el.addWidget(QLabel('Сначала примените параметры развёртки. Armed выбирает канал запуска и хранится в GUI.'))
            elif page == 2:
                self.digital_label = QLabel('D7…D0: —'); el.addWidget(self.digital_label)
                self.build_digital_bits(el)
                el.addWidget(QLabel('DIP switches: нет документированной USB-команды чтения.'))
                el.addWidget(QLabel('USB cable: состояние соединения указано сверху; скорость шины через PDF-команды недоступна.'))
                bind = QCheckBox('Bind to this computer’s IP address (Ethernet only)')
                bind.setEnabled(False); el.addWidget(bind)
                el.addWidget(QLabel('IPLOCK/IPUNLOCK работают при отправке через Ethernet (§6.6.11).'))
            else:
                self.command_input = QLineEdit(); self.command_input.setPlaceholderText('Например: VER, X1., OF. 1000')
                send = QPushButton('Отправить'); row = QHBoxLayout()
                row.addWidget(self.command_input); row.addWidget(send); el.addLayout(row)
                self.command_input.returnPressed.connect(self.raw_command); send.clicked.connect(self.raw_command)
                self.command_status = QLabel('Command valid: — | Parameter valid: —'); el.addWidget(self.command_status)
                row = QHBoxLayout(); el.addLayout(row)
                for text, action in [('Auto Default (ADF 1)', 'ADF 1'), ('Auto Measure', 'ASM')]:
                    b = QPushButton(text); row.addWidget(b); b.clicked.connect(lambda checked=False, a=action: self.action(a))
                el.addWidget(QLabel('Auto Default сбрасывает настройки по нажатию; ADF 1 сохраняет параметры связи.'))
            self.tabs.addTab(scroll, name)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(1000)
        self.log.setMaximumHeight(130); layout.addWidget(self.log)
        self.poll_timer = QTimer(self); self.poll_timer.setSingleShot(True)
        self.poll_timer.timeout.connect(self.poll)
        self.auto_poll.toggled.connect(self.schedule_poll)
        self.tabs.currentChanged.connect(self.page_changed)
        self.thread = QThread(self)
        self.worker = Worker(args.demo, args.serial, args.timeout, self.sections_data, args.max_frequency)
        self.worker.moveToThread(self.thread)
        self.request.connect(self.worker.execute, Qt.QueuedConnection)
        self.stop_worker.connect(self.worker.shutdown, Qt.QueuedConnection)
        self.worker.result.connect(self.on_result); self.worker.failed.connect(self.on_error)
        self.worker.progress.connect(self.statusBar().showMessage)
        self.worker.stopped.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.finished_shutdown)
        self.thread.start()
        self.sensitivity_choices(1)
        self.update_enabled()

    def build_digital_bits(self, layout):
        self.bit_controls = []
        grid = QGridLayout(); layout.addLayout(grid)
        grid.addWidget(QLabel('D7…D0'), 0, 0)
        for row, label in enumerate(['Input direction', 'Output value', 'Read value'], 1):
            grid.addWidget(QLabel(label), row, 0)
        for bit in range(8):
            col = 8-bit
            grid.addWidget(QLabel(f'D{bit}'), 0, col)
            direction, output, value = QCheckBox(), QCheckBox(), QLabel('—')
            grid.addWidget(direction, 1, col); grid.addWidget(output, 2, col)
            grid.addWidget(value, 3, col)
            direction.clicked.connect(self.bits_edited); output.clicked.connect(self.bits_edited)
            self.bit_controls.append((direction, output, value))
        mask_box = self.sections['Digital Port'][0]
        for key in ('portdir', 'byte', 'readbyte'):
            mask_box.widgets[key].valueChanged.connect(self.update_digital_bits)

    def bits_edited(self):
        mask_box = self.sections['Digital Port'][0]
        for index, key in enumerate(('portdir', 'byte')):
            value = sum((1 << bit) for bit, controls in enumerate(self.bit_controls)
                        if controls[index].isChecked())
            w = mask_box.widgets[key]
            w.blockSignals(True); w.setValue(value); w.blockSignals(False)
        self.update_digital_bits()

    def all_boxes(self):
        return [b for boxes in self.sections.values() for b in boxes]

    def sensitivity_choices(self, input_mode):
        imode = max(0, input_mode-3)
        for ch in (1, 2):
            for box in self.sections[f'Reference / Output {ch}']:
                w = box.widgets[f'sen{ch}']; old = w.currentData(); w.clear()
                for code in range(7 if imode == 2 else 3, 28):
                    base = (1, 2, 5)[code % 3] * 10**((code//3)-1)
                    # code=3 -> 1*10^0 *10 nV = 10 nV
                    volts = base*1e-8
                    value = volts * (1 if imode == 0 else 1e-6 if imode == 1 else 1e-8)
                    w.addItem(self.engineering(value, 'V' if imode == 0 else 'A'), code)
                index = w.findData(old)
                w.setCurrentIndex(index if index >= 0 else 0)
                next(f for f in box.fields if f.key == f'sen{ch}').choices = [
                    (w.itemText(i), w.itemData(i)) for i in range(w.count())]

    @staticmethod
    def engineering(value, unit):
        for factor, prefix in [(1, ''), (1e-3, 'm'), (1e-6, 'µ'), (1e-9, 'n'), (1e-12, 'p'), (1e-15, 'f')]:
            if abs(value) >= factor*.999 or factor == 1e-15:
                return f'{value/factor:.5g} {prefix}{unit}'

    def update_enabled(self):
        ready = self.connected and not self.busy and not self.closing
        self.connect_button.setEnabled(not self.connected and not self.busy and not self.closing)
        self.disconnect_button.setEnabled(ready); self.refresh_button.setEnabled(ready)
        self.tabs.setEnabled(ready)
        for box in self.all_boxes():
            box.enable_fields(ready, self.mode, self.legacy.isChecked())

    def submit(self, job, payload=None):
        if self.busy or self.closing or (job != 'open' and not self.connected):
            return False
        self.poll_timer.stop(); self.busy = True
        data = dict(payload or {}); data['legacy'] = self.legacy.isChecked()
        self.update_enabled(); self.request.emit(job, data)
        return True

    def refresh(self):
        titles = self.page_titles[self.tabs.currentIndex()]
        self.submit('read', {'titles': titles})

    def page_changed(self, page):
        if self.connected and not self.busy:
            if page not in self.page_loaded:
                self.refresh()
            else:
                self.schedule_poll()

    def legacy_changed(self):
        self.page_loaded.clear()
        self.update_enabled()
        if self.connected and not self.busy:
            self.refresh()

    def apply_section(self, title):
        box = next(b for b in self.page_sections[self.tabs.currentIndex()] if b.title == title)
        changed = box.changed()
        if not changed:
            self.statusBar().showMessage('Изменений нет'); return
        self.active_title = title
        self.submit('apply', {'title': title, 'values': box.values(), 'changed': changed})

    def action(self, action, channel=1, **kwargs):
        self.submit('action', dict(action=action, channel=channel, **kwargs))

    def start_sweep(self):
        box = self.sections['Frequency / Amplitude Sweep'][0]
        if box.changed():
            self.statusBar().showMessage('Сначала нажмите «Применить» для параметров развёртки'); return
        mask = int(self.arm_frequency.isChecked()) + 2*int(self.arm_amplitude.isChecked())
        self.action('sweep', mask=mask)

    def raw_command(self):
        self.submit('raw', {'command': self.command_input.text()})

    def schedule_poll(self, *unused):
        if self.connected and not self.busy and not self.closing and self.auto_poll.isChecked():
            self.poll_timer.start(self.interval.value())
        else:
            self.poll_timer.stop()

    def poll(self):
        self.submit('poll', {'selections': [(s.currentIndex(), u.isChecked()) for s, u, _ in self.reading_controls],
                             'rear': self.tabs.currentIndex() == 2, 'oscillator': self.tabs.currentIndex() == 1})

    @pyqtSlot(str, object)
    def on_result(self, job, data):
        self.busy = False
        if self.closing:
            return
        if job == 'open':
            self.connected = True; self.page_loaded.clear()
            self.connection_label.setText('Подключено — firmware: '+data['version'])
            self.log.appendPlainText('Соединение открыто. Режим демодуляции не изменён.')
            self.refresh(); return
        if job == 'close':
            self.connected = False; self.page_loaded.clear()
            self.connection_label.setText('Отключено')
            self.clear_readings()
        elif job in ('read', 'apply'):
            for title, state in data.items():
                self.mode = state['mode']
                if title == 'Input' and 'input' in state['values']:
                    self.sensitivity_choices(int(state['values']['input']))
                if title.startswith('Reference / Output') and not self.sections[title][0].widgets[f'sen{title[-1]}'].count():
                    self.sensitivity_choices(1)
                for box in self.sections[title]:
                    box.load(state)
                if state['warnings']:
                    self.log.appendPlainText('\n'.join(state['warnings']))
                if state.get('completed'):
                    self.log.appendPlainText('\n'.join('→ '+c for c in state['completed']))
            if job == 'read':
                self.page_loaded.add(self.tabs.currentIndex())
            self.update_derived()
        elif job == 'poll':
            if data['mode'] != self.mode:
                self.mode = data['mode']; self.page_loaded.clear()
                self.log.appendPlainText('Режим прибора изменился. Перечитайте текущую вкладку.')
            for (_, _, label), value in zip(self.reading_controls, data['readings']):
                label.setText(value)
            for key, value in data.get('extras', {}).items():
                if key == 'sweep':
                    self.start_button.setText(f'Sweep status: {value} — Start')
                else:
                    title = 'ADCs' if key.startswith('adc') else 'Digital Port'
                    for box in self.sections[title]:
                        box.widgets[key].setValue(value)
            self.update_digital_bits()
            st, ov = data['status'], data['overload']
            freq = ' / '.join(f'{f:.8g} Hz' for f in data['frequency'])
            oa, of = data['oscillator']
            self.live_label.setText(f'ST: 0x{st:02X} | Overload: 0x{ov:02X} | Reference: {freq} | '
                                    f'Oscillator: {oa:.6f} V rms, {of:.3f} Hz'+
                                    (' | Reference unlock' if st & 8 else '')+
                                    (' | Input overload' if st & 64 else ''))
            self.live_label.setStyleSheet('color: #b3261e;' if st & 88 or ov else '')
        elif job == 'raw':
            cmd, answer, st, ov = data
            self.log.appendPlainText(f'→ {cmd}\n← {answer or "OK"} | ST=0x{st:02X}, OV=0x{ov:02X}')
            self.command_status.setText(f'Command valid: {not bool(st & 2)} | Parameter valid: {not bool(st & 6)}')
            self.command_input.clear(); self.page_loaded.clear()
            # A raw command may have changed USBTERM. Reconnect before subsequent queries.
            if cmd.split()[0].upper() in ('USBTERM', 'ADF') and len(cmd.split()) > 1:
                self.submit('close'); return
            if not st & 6:
                self.refresh(); return
        elif job == 'action':
            self.log.appendPlainText('Выполнено: '+data['action'])
            self.page_loaded.clear(); self.refresh(); return
        if job == 'apply':
            self.page_loaded.clear()
            if self.active_title in ('Input', 'Demodulator', 'Output filters / Noise'):
                self.refresh(); return
        self.statusBar().showMessage('Готово')
        self.update_enabled(); self.schedule_poll()

    def update_derived(self):
        inp = self.sections['Input'][0].values()
        out = self.sections['Reference / Output 1'][0].values()
        gain, sen = inp['acgain'], out['sen1']
        if gain is not None and sen is not None:
            imode = max(0, int(inp['input'])-3)
            limit = 2.5 / 2**int(gain) * (1 if imode == 0 else 1e-6 if imode == 1 else 1e-8)
            dr = min(100, (5, 19, 11)[int(sen)%3]-int(gain)*6 + ((27-int(sen))//3)*20)
            self.input_info.setText('Input limit ≈ '+self.engineering(limit, 'V pk' if imode == 0 else 'A pk')+
                                    f' | Dynamic reserve (HTML): {dr} dB')
        self.update_digital_bits()

    def update_digital_bits(self):
        port = self.sections['Digital Port'][0].values()
        for bit, (direction, output, value) in enumerate(self.bit_controls):
            direction.setChecked(bool(int(port['portdir']) & (1 << bit)))
            output.setChecked(bool(int(port['byte']) & (1 << bit)))
            output.setEnabled(not direction.isChecked())
            value.setText(str(int(bool(int(port['readbyte']) & (1 << bit)))))
        self.digital_label.setText('D7…D0 | DIR '+format(int(port['portdir']), '08b')+
                                   ' | OUT '+format(int(port['byte']), '08b')+' | IN '+format(int(port['readbyte']), '08b'))

    def clear_readings(self):
        for _, _, label in self.reading_controls:
            label.setText('—')
        self.live_label.setText('ST: — | Overload: — | Reference: —')
        for box in self.all_boxes():
            box.available.clear(); box.note.setText('Нет актуальных данных')

    @pyqtSlot(str, str, bool)
    def on_error(self, job, message, fatal):
        self.busy = False
        if self.closing:
            return
        self.log.appendPlainText(f'Ошибка ({job}): {message}')
        self.statusBar().showMessage(message)
        if fatal or job == 'open':
            self.connected = False; self.page_loaded.clear()
            self.connection_label.setText('Связь закрыта: '+message)
            self.clear_readings()
        elif job == 'apply':
            # A multi-command update can have partially succeeded.
            self.refresh(); return
        self.update_enabled(); self.schedule_poll()

    def closeEvent(self, event):
        if self.thread_stopped:
            event.accept(); return
        event.ignore()
        if not self.closing:
            self.closing = True; self.poll_timer.stop()
            self.update_enabled(); self.statusBar().showMessage('Закрытие USB…')
            self.stop_worker.emit()

    @pyqtSlot()
    def finished_shutdown(self):
        self.thread_stopped = True
        self.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true', help='GUI без VISA и прибора')
    parser.add_argument('--serial', help='USB serial; по умолчанию из вашей библиотеки')
    parser.add_argument('--timeout', type=int, default=2000, help='USB timeout, ms')
    parser.add_argument('--max-frequency', type=int, choices=(120000, 250000), default=120000,
                        help='250000 только с опцией 7230/99')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout должен быть положительным')
    app = QApplication(sys.argv[:1])
    app.setStyle('Fusion')
    window = LockinWindow(args); window.show()
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
