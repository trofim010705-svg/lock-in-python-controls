"""Exclusive lock-in snapshot session; all methods run on the existing USB worker."""
import math


SNAPSHOT_MASK = (1 << 0) | (1 << 1) | (1 << 17) | (1 << 18)  # 393219


class LockInAcquisition:
    def __init__(self, instrument, demo=False):
        self.inst, self.demo = instrument, demo
        self.saved = None
        self.method = 'paired'
        self.metadata = {}

    def prepare(self):
        mode = int(self.inst.values('REFMODE')[0])
        if self.demo and mode != 2:
            self.inst.query('REFMODE 2')
            mode = 2
        if mode != 2:
            raise ValueError('Выберите Dual reference во вкладке «Управление Lock-in» перед сканированием.')
        self.inst.mode = mode
        self.metadata = dict(mode='Dual reference', imode=int(self.inst.values('IMODE')[0]), simulation=self.demo)
        self.metadata['unit'] = 'V' if not self.metadata['imode'] else 'A'
        for command in ('SEN1', 'SEN2', 'TC1', 'TC2', 'REFP1.', 'REFP2.', 'FRQ1.', 'FRQ2.'):
            self.metadata[command] = self.inst.values(command)[0]
        # '?' samples all selected values simultaneously. Preserve any existing curves.
        # Changing CBD resets curve counters; only do so for a completely empty, idle buffer.
        try:
            monitor = self.inst.values('M')
            if len(monitor) == 4 and monitor[0] == 0 and monitor[3] == 0:
                cbd = int(self.inst.values('CBD')[0])
                curve_mode = int(self.inst.values('CMODE')[0])
                if cbd == SNAPSHOT_MASK:
                    self.method = 'snapshot'
                elif curve_mode == 0:
                    self.saved = (cbd, int(self.inst.values('LEN')[0]))
                    self.metadata['original_curve_config'] = dict(CBD=self.saved[0], LEN=self.saved[1])
                    self.inst.query(f'CBD {SNAPSHOT_MASK}')
                    self.method = 'snapshot'
            if self.method == 'snapshot':
                self._read_four()  # Probe support before any stage motion.
        except Exception as exc:
            if type(exc).__name__ != 'CommandError' and not isinstance(exc, ValueError):
                raise
            self.restore()
            self.method = 'paired'
            self.metadata['snapshot_fallback'] = str(exc)
        self.metadata['acquisition'] = self.method
        self.metadata['CBD_temporarily_changed'] = self.saved is not None
        return self.metadata

    def _read_four(self):
        status = overload = 0
        if self.method == 'snapshot':
            values = self.inst.values('?')
            status, overload = self.inst.status, self.inst.overload
        else:
            values = []
            for command in ('XY1.', 'XY2.'):
                pair = self.inst.values(command)
                if len(pair) != 2:
                    raise ValueError(command+': ожидались два числа')
                values.extend(pair)
                status |= self.inst.status
                overload |= self.inst.overload
        if len(values) != 4 or not all(math.isfinite(x) for x in values):
            raise ValueError('Локин вернул некорректный набор X1/Y1/X2/Y2')
        return dict(values=values, status=status, overload=overload)

    def sample(self, position):
        if self.demo:
            self.inst.transport.scan_position = position
        return self._read_four()

    def restore(self):
        saved = self.saved
        if saved is not None:
            # Restore the definition first, then the original length (manual §6.6.10).
            self.inst.query(f'CBD {saved[0]}')
            self.inst.query(f'LEN {saved[1]}')
            self.saved = None
