"""Multiprocessing-only helper. Do not import GUI libraries in this executable."""
import multiprocessing
import multiprocessing.spawn
import os
from pathlib import Path
import sys
import traceback


_worker_paths=list(sys.path)
_original_prepare=multiprocessing.spawn.prepare


def _prepare_worker(data):
    # The GUI has a different frozen module archive. multiprocessing normally
    # replaces sys.path with the parent's paths, hiding this worker's modules.
    data=dict(data);data['sys_path']=_worker_paths
    return _original_prepare(data)


if __name__=='__main__':
    log_folder=os.environ.get('OPTICAL_NI_STARTUP_LOGDIR')
    if log_folder:
        folder=Path(log_folder);folder.mkdir(parents=True,exist_ok=True)
        log=(folder/f'worker_{os.getpid()}.log').open('w',encoding='utf-8',buffering=1)
        sys.stdout=sys.stderr=log
        print('Executable:',sys.executable,'Args:',sys.argv,'Bundle:',getattr(sys,'_MEIPASS',None),flush=True)
    try:
        multiprocessing.spawn.prepare=_prepare_worker
        multiprocessing.freeze_support()
    except SystemExit:
        raise
    except BaseException:
        if log_folder:traceback.print_exc()
        raise SystemExit(1)
    raise SystemExit('NI worker is started by OpticalMagneticLab.exe, not manually.')
