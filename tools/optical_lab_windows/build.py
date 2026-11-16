"""Build the verified OpticalMagneticLab 1.0 source archive on Windows x64."""
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
from zipfile import ZipFile, ZIP_DEFLATED

SOURCE_SHA256 = 'be249e221af2fb3e69f9fd780b141dfb08201cdc9dd774814ea529a7df3115b8'
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
        raise
    data = json.loads(report.read_text(encoding='utf-8'))
    if not data.get('ok') or not data.get('frozen') or data.get('qt_platform') != 'windows':
        raise RuntimeError(f'Frozen Windows GUI check failed: {data}')
    for flag in ('connect_does_not_write_ao','gain_live_verified','dual_internal_harmonics_verified','full_point_photo_normalization_verified',
                 'arcseconds_verified','faraday_centered_verified','raw_daq_stream_verified',
                 'hysteresis_hold_verified','stop_preserves_ao_verified','explicit_finish_zero_verified',
                 'four_twin_axes_and_time_plots','close_preserved_ao','threads_stopped'):
        assert data.get(flag), (flag, data)
    print(json.dumps(data, ensure_ascii=True), flush=True)
    return data


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
    local_source = script.with_name('OpticalMagneticLab_v1.zip')
    archive_folder = script.parent if local_source.exists() else repo / 'updates'
    parser.add_argument('--source', type=Path, default=archive_folder / 'OpticalMagneticLab_v1.zip')
    parser.add_argument('--output', type=Path, default=archive_folder / 'OpticalMagneticLab_v1_Windows_x64.zip')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if not args.prepare_only and (sys.platform != 'win32' or platform.architecture()[0] != '64bit'):
        parser.error('Run the Windows x64 build on Windows with Python 3.11 x64.')
    with tempfile.TemporaryDirectory(prefix='optical_lab_build_') as temporary:
        work = Path(temporary)
        source = extract_source(args.source.resolve(), work / 'source')
        launcher = source / 'optical_lab_exe_launcher.py'
        shutil.copy2(Path(__file__).with_name('launcher.py'), launcher)
        if args.prepare_only:
            print('Source hash, extraction paths, and launcher preparation: OK')
            return
        # Build from the extracted application directory, not the repo's lock-in app.py.
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                       cwd=source, check=True, timeout=120)
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                        '--onedir', '--windowed', '--collect-all', 'nidaqmx',
                        '--recursive-copy-metadata', 'nidaqmx', '--collect-all', 'pyvisa',
                        '--hidden-import', 'lockin_7230_usb',
                        '--hidden-import', 'matplotlib.backends.backend_pdf', '--name', NAME,
                        '--distpath', str(work / 'dist'), '--workpath', str(work / 'build'),
                        '--specpath', str(work / 'spec'), str(launcher)],
                       cwd=source, check=True, timeout=600)
        bundle = work / 'dist' / NAME
        exe = bundle / f'{NAME}.exe'
        pe = exe.read_bytes()
        offset = struct.unpack_from('<I', pe, 0x3c)[0]
        assert pe[:2] == b'MZ' and pe[offset:offset+4] == b'PE\0\0'
        assert struct.unpack_from('<H', pe, offset+4)[0] == 0x8664
        smoke = test_executable(exe, work / 'smoke.json')
        # Check loading Qt DLLs and its platform plugin from a Cyrillic directory too.
        unicode_bundle = work / 'Проверка_пути' / NAME
        shutil.copytree(bundle, unicode_bundle)
        unicode_smoke = test_executable(unicode_bundle / exe.name, work / 'smoke_unicode.json')
        print('Frozen application passed checks in ASCII and Cyrillic directories.', flush=True)
        versions = {name: importlib.metadata.version(name) for name in
                    ('PyQt5', 'PyQt5-Qt5', 'PyQt5-sip', 'numpy', 'pyqtgraph', 'nidaqmx', 'nitypes', 'PyVISA', 'pyserial', 'matplotlib', 'Pillow', 'PyInstaller')}
        manifest = {'application': NAME, 'version': '1.0', 'target': 'Windows x64',
                    'source_archive_sha256': SOURCE_SHA256, 'exe_sha256': sha256(exe),
                    'packages': versions, 'smoke': smoke, 'unicode_path_smoke': unicode_smoke}
        (bundle / 'build_info.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        # Include the exact application and build sources alongside the binary.
        source_copy = bundle / 'Source'
        shutil.copytree(source, source_copy, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copy2(Path(__file__), source_copy / 'build_windows.py')
        shutil.copy2(Path(__file__).with_name('launcher.py'), source_copy / 'launcher.py')
        shutil.copy2(args.source, source_copy / args.source.name)
        shutil.copy2(Path(__file__).with_name('requirements-build.txt'), source_copy / 'requirements-build.txt')
        shutil.copy2(Path(__file__).with_name('START_HERE_RU.txt'), bundle / 'START_HERE_RU.txt')
        shutil.copy2(Path(__file__).with_name('START_HERE_RU.txt'), source_copy / 'START_HERE_RU.txt')
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
