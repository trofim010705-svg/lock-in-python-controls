"""Build OpticalMagneticLab 1.1 with an independent Qt-free NI worker."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import struct
import tempfile
import os
from zipfile import ZipFile, ZIP_DEFLATED

SOURCE_SHA256 = '261aa21db86ff1c630fd92040460e3fd1898eed2fd9a7e9dd72509719fb6326e'
NAME = 'OpticalMagneticLab'


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_source(archive, destination):
    if sha256(archive) != SOURCE_SHA256:
        raise RuntimeError('Source archive hash mismatch: refusing to build another version.')
    with ZipFile(archive) as z:
        for item in z.infolist():
            target = (destination / item.filename).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError('Unsafe path in source ZIP.')
        z.extractall(destination)
    root = destination / NAME
    if not (root / 'app.py').is_file():
        raise RuntimeError('Source app.py missing.')
    return root


def test_executable(exe, report):
    try:
        subprocess.run([str(exe), '--self-test', str(report)], check=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        detail = json.loads(report.read_text(encoding='utf-8')) if report.exists() else {'error': 'report not created'}
        print('Frozen check failed; report: ' + json.dumps({k: v for k, v in detail.items() if k != 'journal'}, ensure_ascii=True), flush=True)
        dump_worker_logs()
        raise
    data = json.loads(report.read_text(encoding='utf-8'))
    if not data.get('ok') or not data.get('frozen') or data.get('qt_platform') != 'windows':
        raise RuntimeError(f'Frozen Windows GUI check failed: {data}')
    for flag in ('ni_children_without_qt','connect_does_not_write_ao','gain_live_verified','dual_internal_harmonics_verified','full_point_photo_normalization_verified',
                 'arcseconds_verified','faraday_centered_verified','raw_daq_stream_verified',
                 'hysteresis_hold_verified','stop_preserves_ao_verified','explicit_finish_zero_verified',
                 'four_twin_axes_and_time_plots','close_preserved_ao','threads_stopped'):
        assert data.get(flag), (flag, data)
    print(json.dumps(data, ensure_ascii=True), flush=True)
    return data


def dump_worker_logs():
    folder=os.environ.get('OPTICAL_NI_STARTUP_LOGDIR')
    if folder:
        for p in sorted(Path(folder).glob('*.log')):
            print('Worker startup log '+p.name+':\n'+p.read_text(encoding='utf-8')[-12000:],flush=True)


def copy_licenses(destination):
    target = destination / 'Licenses'
    for name in ('PyQt5', 'PyQt5-Qt5', 'PyQt5-sip', 'numpy', 'pyqtgraph', 'nidaqmx', 'nitypes', 'PyVISA', 'pyserial', 'matplotlib', 'Pillow', 'PyInstaller'):
        distribution = importlib.metadata.distribution(name)
        for entry in distribution.files or ():
            if any(token in Path(entry).name.lower() for token in ('license', 'copying', 'copyright')):
                source = Path(distribution.locate_file(entry))
                if source.is_file() and source.stat().st_size < 2_000_000:
                    folder = target / name
                    folder.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, folder / source.name)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.exists():
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(python_license, target / 'Python-LICENSE.txt')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    script = Path(__file__).resolve()
    repo = script.parents[2] if len(script.parents) > 2 else script.parent
    local_source = script.with_name('OpticalMagneticLab_v1_1.zip')
    archive_folder = script.parent if local_source.exists() else repo / 'updates'
    parser.add_argument('--source', type=Path, default=archive_folder / 'OpticalMagneticLab_v1_1.zip')
    parser.add_argument('--output', type=Path, default=archive_folder / 'OpticalMagneticLab_v1_1_Windows_x64.zip')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if not args.prepare_only and (sys.platform != 'win32' or platform.architecture()[0] != '64bit'):
        parser.error('Run the Windows x64 build on Windows with Python 3.11 x64.')
    with tempfile.TemporaryDirectory(prefix='optical_lab_build_') as temporary:
        work = Path(temporary)
        os.environ['OPTICAL_NI_STARTUP_LOGDIR']=str(work/'worker_logs')
        source = extract_source(args.source.resolve(), work / 'source')
        launcher = source / 'optical_lab_exe_launcher.py'
        shutil.copy2(Path(__file__).with_name('launcher.py'), launcher)
        if args.prepare_only:
            print('Source hash, extraction paths, and launcher preparation: OK')
            return
        # Build from the extracted application directory, not the repo's lock-in app.py.
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                       cwd=source, check=True, timeout=120)
        worker_launcher=source/'optical_ni_worker_launcher.py'
        shutil.copy2(Path(__file__).with_name('ni_worker_launcher.py'),worker_launcher)
        subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--windowed',
                        '--collect-all','nidaqmx','--recursive-copy-metadata','nidaqmx',
                        '--hidden-import','ni_native','--hidden-import','ni_diagnostics','--hidden-import','models',
                        '--exclude-module','PyQt5','--exclude-module','PyQt6','--exclude-module','PySide2',
                        '--exclude-module','PySide6','--exclude-module','matplotlib','--exclude-module','pyqtgraph',
                        '--exclude-module','pyvisa','--name','OpticalNIWorker',
                        '--distpath',str(work/'worker_dist'),'--workpath',str(work/'worker_build'),
                        '--specpath',str(work/'worker_spec'),str(worker_launcher)],
                       cwd=source,check=True,timeout=600)
        worker_bundle=work/'worker_dist'/'OpticalNIWorker'
        assert not any('qt' in p.name.lower() for p in worker_bundle.rglob('*') if p.suffix.lower() in ('.dll','.pyd'))
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                        '--onedir', '--windowed', '--collect-all', 'nidaqmx',
                        '--recursive-copy-metadata', 'nidaqmx', '--collect-all', 'pyvisa',
                        '--hidden-import', 'lockin_7230_usb',
                        '--hidden-import', 'matplotlib.backends.backend_pdf', '--name', NAME,
                        '--distpath', str(work / 'dist'), '--workpath', str(work / 'build'),
                        '--specpath', str(work / 'spec'), str(launcher)],
                       cwd=source, check=True, timeout=600)
        bundle = work / 'dist' / NAME
        shutil.copytree(worker_bundle,bundle/'NIWorker')
        exe = bundle / f'{NAME}.exe'
        pe = exe.read_bytes()
        offset = struct.unpack_from('<I', pe, 0x3c)[0]
        assert pe[:2] == b'MZ' and pe[offset:offset+4] == b'PE\0\0'
        assert struct.unpack_from('<H', pe, offset+4)[0] == 0x8664
        diagnostic_report=work/'ni_diagnostics.txt'
        diagnostic_process=subprocess.run([str(exe),'--ni-diagnostics',str(diagnostic_report)],timeout=90)
        assert diagnostic_process.returncode in (0,1)
        diagnostic=json.loads(diagnostic_report.read_text(encoding='utf-8'))
        assert diagnostic['output_writes']==0 and diagnostic['device_resets']==0
        assert len(diagnostic['checks'])==2
        dump_worker_logs()
        print('Isolated worker diagnostic: '+json.dumps(diagnostic,ensure_ascii=True),flush=True)
        assert all('runtime' in check and not check['runtime']['qt_loaded'] for check in diagnostic['checks'])
        smoke = test_executable(exe, work / 'smoke.json')
        # Check loading Qt DLLs and its platform plugin from a Cyrillic directory too.
        unicode_bundle = work / 'Проверка_пути' / NAME
        shutil.copytree(bundle, unicode_bundle)
        unicode_smoke = test_executable(unicode_bundle / exe.name, work / 'smoke_unicode.json')
        print('Frozen application passed checks in ASCII and Cyrillic directories.', flush=True)
        versions = {name: importlib.metadata.version(name) for name in
                    ('PyQt5', 'PyQt5-Qt5', 'PyQt5-sip', 'numpy', 'pyqtgraph', 'nidaqmx', 'nitypes', 'PyVISA', 'pyserial', 'matplotlib', 'Pillow', 'PyInstaller')}
        manifest = {'application': NAME, 'version': '1.1', 'target': 'Windows x64',
                    'source_archive_sha256': SOURCE_SHA256, 'exe_sha256': sha256(exe),
                    'ni_worker_sha256':sha256(bundle/'NIWorker'/'OpticalNIWorker.exe'),
                    'packages': versions, 'smoke': smoke, 'unicode_path_smoke': unicode_smoke,
                    'ni_diagnostics_smoke':diagnostic}
        (bundle / 'build_info.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        # Include the exact application and build sources alongside the binary.
        source_copy = bundle / 'Source'
        shutil.copytree(source, source_copy, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copy2(Path(__file__), source_copy / 'build_windows.py')
        shutil.copy2(Path(__file__).with_name('launcher.py'), source_copy / 'launcher.py')
        shutil.copy2(Path(__file__).with_name('ni_worker_launcher.py'),source_copy/'ni_worker_launcher.py')
        shutil.copy2(args.source, source_copy / args.source.name)
        shutil.copy2(Path(__file__).with_name('requirements-build.txt'), source_copy / 'requirements-build.txt')
        shutil.copy2(Path(__file__).with_name('START_HERE_RU.txt'), bundle / 'START_HERE_RU.txt')
        shutil.copy2(Path(__file__).with_name('START_HERE_RU.txt'), source_copy / 'START_HERE_RU.txt')
        for filename in ('diagnose_ni.bat','start_ni_classic.bat'):
            shutil.copy2(source/filename,bundle/filename)
        copy_licenses(bundle)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(args.output, 'w', ZIP_DEFLATED) as z:
            for path in sorted(bundle.rglob('*')):
                if path.is_file():
                    z.write(path, Path(NAME) / path.relative_to(bundle))
        with ZipFile(args.output) as check:
            assert check.testzip() is None
        checksum = sha256(args.output)
        args.output.with_suffix('.sha256').write_text(f'{checksum}  {args.output.name}\n', encoding='ascii')
        print(f'Built: {args.output}; {args.output.stat().st_size} bytes; SHA256={checksum}', flush=True)


if __name__ == '__main__':
    main()
