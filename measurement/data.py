"""Experiment parameters and non-overwriting paired data/metadata output."""
from dataclasses import asdict,dataclass
from datetime import datetime,timezone
from pathlib import Path
import json
import math
import os
import tempfile


@dataclass(frozen=True)
class Parameters:
    sig_mv: float
    bal_mv: float
    acquisition_time_s: float
    delay_ms: float
    e_field: str = ''
    h_field: str = ''
    additional: str = ''

    def validate(self):
        if not all(math.isfinite(x) for x in (self.sig_mv,self.bal_mv,self.acquisition_time_s,self.delay_ms)):
            raise ValueError('Все числовые параметры должны быть конечными.')
        if not .01<=self.acquisition_time_s<=604800:
            raise ValueError('Acquisition time: от 0.01 s до 7 суток.')
        if not 1<=self.delay_ms<=3600000:
            raise ValueError('Delay: от 1 ms до 1 часа.')


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_pair(folder,metadata,rows):
    """Only called after a cycle. Keep old files intact and retryable on failure."""
    folder=Path(folder).expanduser();folder.mkdir(parents=True,exist_ok=True)
    stamp=datetime.fromisoformat(metadata['started_utc']).strftime('%Y%m%d_%H%M%S_%f')
    temporary=[];published=[];reservation=None
    try:
        for number in range(10000):
            name='lockin_'+stamp+('' if number==0 else f'_{number:03d}')
            data=folder/(name+'.dat');notes=folder/(name+'.txt')
            reservation=folder/('.'+name+'.reserve')
            if data.exists() or notes.exists():continue
            try:
                fd=os.open(reservation,os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.close(fd);break
            except FileExistsError:continue
        else:raise OSError('Не удалось подобрать свободное имя файлов.')
        unit=metadata.get('lockin',{}).get('magnitude_unit','unknown')
        for kind,target in (('data',data),('notes',notes)):
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',newline='\n',dir=folder,
                                             prefix='.'+name+'_',suffix='.tmp',delete=False) as stream:
                temporary.append(Path(stream.name))
                if kind=='data':
                    stream.write(f'Magn1_{unit}\tPhase1_deg\n')
                    for t0,t1,magnitude,phase,status,overload in rows:
                        stream.write(f'{magnitude:.17g}\t{phase:.17g}\n')
                else:
                    stream.write('SIGNAL RECOVERY 7230 · TIMED ACQUISITION\n')
                    stream.write('Magn1: '+unit+'; Phase1: degrees, as returned by the instrument.\n')
                    stream.write('CONFIGURATION AND RESULT\n')
                    stream.write(json.dumps(metadata,ensure_ascii=False,indent=2,allow_nan=False))
                    stream.write('\n\nSAMPLE TIMING / STATUS\n')
                    stream.write('Index\tRead_start_s\tRead_end_s\tStatus\tOverload\n')
                    for i,(t0,t1,mag,phase,status,overload) in enumerate(rows):
                        stream.write(f'{i}\t{t0:.12g}\t{t1:.12g}\t0x{status:02X}\t0x{overload:02X}\n')
                stream.flush();os.fsync(stream.fileno())
        for source,target in zip(temporary,(data,notes)):
            os.replace(source,target);published.append(target)
        return dict(data=str(data.resolve()),metadata=str(notes.resolve()))
    except Exception:
        for path in published:path.unlink(missing_ok=True)
        raise
    finally:
        for path in temporary:path.unlink(missing_ok=True)
        # Only remove a reservation successfully created by this call.
        if reservation is not None and 'fd' in locals():reservation.unlink(missing_ok=True)
