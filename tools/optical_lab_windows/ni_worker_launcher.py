"""Multiprocessing-only helper. Do not import GUI libraries in this executable."""
import multiprocessing


if __name__=='__main__':
    multiprocessing.freeze_support()
    raise SystemExit('NI worker is started by OpticalMagneticLab.exe, not manually.')
