"""Scan coordinates and incremental, non-overwriting text-file storage."""
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class ScanConfig:
    start: float
    stop: float
    step: float
    delay_ms: float
    output: str
    comments: str = ''
    tolerance_mm: float = .002
    motion_timeout_s: float = 120
    stop_on_warning: bool = True

    def point_count(self):
        numbers = (self.start, self.stop, self.step, self.delay_ms, self.tolerance_mm, self.motion_timeout_s)
        if not all(math.isfinite(x) for x in numbers):
            raise ValueError('Все параметры сканирования должны быть конечными числами')
        if self.step <= 0 or self.delay_ms < 0 or self.tolerance_mm <= 0 or self.motion_timeout_s <= 0:
            raise ValueError('Шаг, допуск и тайм-аут должны быть > 0; задержка ≥ 0')
        start, stop, step = map(lambda x: Decimal(str(x)), (self.start, self.stop, self.step))
        count = int(abs(stop-start)//step)+1
        if count > 500000:
            raise ValueError('Слишком много точек: максимум 500000. Увеличьте шаг.')
        if abs(stop-start) % step:
            count += 1
        if count > 500000:
            raise ValueError('Превышен предел 500000 точек')
        return count

    def positions(self):
        count = self.point_count()
        start, stop, step = map(lambda x: Decimal(str(x)), (self.start, self.stop, self.step))
        direction = 1 if stop >= start else -1
        values = [float(start+direction*step*i) for i in range(count-1)]
        values.append(self.stop)
        return values



class ScanWriter:
    def __init__(self, config, metadata):
        self.config, self.metadata = config, metadata
        self.data = self.notes = None
        self.path = self.comments_path = None
        self.count = 0

    def open(self):
        requested = Path(self.config.output).expanduser()
        if requested.suffix.lower() not in ('.dat', '.txt'):
            raise ValueError('Выберите файл .dat или .txt')
        requested.parent.mkdir(parents=True, exist_ok=True)
        for index in range(10000):
            path = requested if index == 0 else requested.with_name(f'{requested.stem}_{index:03d}{requested.suffix}')
            comments_path = path.with_name(path.stem+'.comments.txt')
            if comments_path.exists():
                continue
            try:
                data = path.open('x', encoding='utf-8', newline='\r\n')
            except FileExistsError:
                continue
            try:
                notes = comments_path.open('x', encoding='utf-8', newline='\n')
            except FileExistsError:
                data.close(); path.unlink(); continue
            except Exception:
                data.close(); path.unlink(); raise
            self.path, self.comments_path, self.data, self.notes = path, comments_path, data, notes
            break
        else:
            raise OSError('Не удалось выбрать свободное имя файла')
        try:
            # Origin: first row = Long Names, second = Units, then numeric TSV.
            unit = self.metadata['lockin']['unit']
            self.data.write('Position\tX1\tY1\tX2\tY2\n')
            self.data.write('mm\t'+'\t'.join([unit]*4)+'\n')
            self.notes.write('Pump–Probe / Spin dynamics\nStarted (UTC): '+datetime.now(timezone.utc).isoformat()+'\n\n')
            self.notes.write('COMMENTS\n'+self.config.comments+'\n\nCONFIGURATION\n')
            self.notes.write(json.dumps(dict(scan=asdict(self.config), **self.metadata), ensure_ascii=False, indent=2)+'\n')
            self.data.flush(); self.notes.flush()
        except Exception:
            self.close('error', 'Не удалось записать заголовок файла')
            raise
        return str(self.path)

    def append(self, position, values):
        if len(values) != 4 or not all(math.isfinite(x) for x in (position, *values)):
            raise ValueError('Некорректные данные измерения')
        offset = self.data.tell()
        try:
            self.data.write(f'{position:.9f}\t'+'\t'.join(f'{value:.12g}' for value in values)+'\n')
            self.data.flush()
        except Exception:
            # Keep the earlier complete rows if a disk write fails midway through a row.
            try:
                self.data.seek(offset); self.data.truncate()
            except OSError:
                pass
            raise
        self.count += 1

    def event(self, text):
        if self.notes:
            self.notes.write(datetime.now(timezone.utc).isoformat()+' '+text+'\n')
            self.notes.flush()

    def close(self, outcome, message=''):
        errors = []
        if self.notes:
            try:
                self.notes.write(f'\nRESULT: {outcome}\nPoints saved: {self.count}\n{message}\n')
                self.notes.flush(); os.fsync(self.notes.fileno())
            except OSError as exc:
                errors.append(str(exc))
        for handle in (self.data, self.notes):
            if handle:
                try:
                    handle.flush(); os.fsync(handle.fileno())
                except OSError as exc:
                    errors.append(str(exc))
                finally:
                    try:
                        handle.close()
                    except OSError as exc:
                        errors.append(str(exc))
        self.data = self.notes = None
        if errors:
            raise OSError('Ошибка завершения записи: '+'; '.join(errors))
