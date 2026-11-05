"""Build and verify the independent Magnetic Field Analyzer on native Windows x64."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED

NAME='MagneticFieldAnalyzer'
SOURCE_SHA256='62ae6902e094c7f90906fd82a44f7e0568a67e0cc1e961e525b6e3554b46ba4c'


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def extract(archive, target):
    if sha256(archive)!=SOURCE_SHA256:
        raise RuntimeError('Source hash mismatch.')
    with ZipFile(archive) as z:
        for item in z.infolist():
            if not (target/item.filename).resolve().is_relative_to(target.resolve()):
                raise RuntimeError('Unsafe source ZIP path.')
        z.extractall(target)
    source=target/NAME
    assert (source/'app.py').is_file()
    return source


def smoke(exe, report):
    try:
        subprocess.run([str(exe),'--self-test',str(report)],check=True,timeout=60)
    except (subprocess.TimeoutExpired,subprocess.CalledProcessError):
        print(report.read_text(encoding='utf-8') if report.exists() else 'No self-test report.',flush=True)
        raise
    result=json.loads(report.read_text(encoding='utf-8'))
    assert result['ok'] and result['frozen'] and result['qt_platform']=='windows',result
    assert result['independent_phase_sliders'] and result['same_figure_export_and_zoom']
    assert result['source_files_unchanged']
    print(json.dumps(result,ensure_ascii=True),flush=True)
    return result


def licenses(bundle):
    for name in ('PyQt5','PyQt5-Qt5','PyQt5-sip','numpy','matplotlib','Pillow','cycler',
                 'contourpy','kiwisolver','fonttools','python-dateutil','six','packaging',
                 'PyInstaller','pyinstaller-hooks-contrib'):
        dist=importlib.metadata.distribution(name)
        for entry in dist.files or ():
            if any(word in Path(entry).name.lower() for word in ('license','copying','copyright')):
                p=Path(dist.locate_file(entry))
                if p.is_file() and p.stat().st_size<2_000_000:
                    target=bundle/'Licenses'/name
                    target.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(p,target/p.name)
    python_license=Path(sys.base_prefix)/'LICENSE.txt'
    if python_license.exists():
        shutil.copy2(python_license,bundle/'Licenses'/'Python-LICENSE.txt')


def main():
    script=Path(__file__).resolve()
    repo=script.parents[2]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=repo/'updates/MagneticFieldAnalyzer_v1.zip')
    parser.add_argument('--output',type=Path,default=repo/'updates/MagneticFieldAnalyzer_v1_Windows_x64.zip')
    parser.add_argument('--prepare-only',action='store_true')
    args=parser.parse_args()
    if not args.prepare_only and (sys.platform!='win32' or platform.architecture()[0]!='64bit'):
        parser.error('Build with Python 3.11 x64 on Windows.')
    with tempfile.TemporaryDirectory(prefix='field_analyzer_build_') as tmp:
        work=Path(tmp)
        source=extract(args.source,work/'source')
        entry=source/'field_analyzer_launcher.py'
        shutil.copy2(script.with_name('launcher.py'),entry)
        if args.prepare_only:
            print('Verified source archive hash, paths, and launcher.')
            return
        subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],cwd=source,check=True,timeout=120)
        subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--windowed',
            '--name',NAME,'--hidden-import','matplotlib.backends.backend_pdf',
            '--hidden-import','matplotlib.backends.backend_svg',
            '--exclude-module','tkinter','--exclude-module','PyQt6','--exclude-module','PySide6',
            '--exclude-module','PySide2','--distpath',str(work/'dist'),'--workpath',str(work/'build'),
            '--specpath',str(work/'spec'),str(entry)],cwd=source,check=True,timeout=600)
        bundle=work/'dist'/NAME
        exe=bundle/(NAME+'.exe')
        pe=exe.read_bytes(); offset=struct.unpack_from('<I',pe,0x3c)[0]
        assert pe[:2]==b'MZ' and pe[offset:offset+4]==b'PE\0\0'
        assert struct.unpack_from('<H',pe,offset+4)[0]==0x8664
        first=smoke(exe,work/'ascii_test'/'report.json')
        unicode_bundle=work/'Проверка_пути'/NAME
        shutil.copytree(bundle,unicode_bundle)
        second=smoke(unicode_bundle/exe.name,work/'тест_кириллица'/'report.json')
        print('Frozen Windows application passed ASCII and Cyrillic path checks.',flush=True)
        versions={name:importlib.metadata.version(name) for name in ('PyQt5','PyQt5-Qt5','numpy','matplotlib','Pillow','PyInstaller')}
        metadata={'application':NAME,'version':'1.0','target':'Windows x64',
            'source_archive_sha256':SOURCE_SHA256,'exe_sha256':sha256(exe),
            'packages':versions,'smoke':first,'unicode_path_smoke':second}
        (bundle/'build_info.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
        source_copy=bundle/'Source'
        shutil.copytree(source,source_copy,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        for original,new in ((script,'build_windows.py'),(script.with_name('launcher.py'),'launcher.py'),
                             (script.with_name('requirements-build.txt'),'requirements-build.txt')):
            shutil.copy2(original,source_copy/new)
        shutil.copy2(args.source,source_copy/args.source.name)
        shutil.copy2(script.with_name('START_HERE_RU.txt'),bundle/'START_HERE_RU.txt')
        licenses(bundle)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with ZipFile(args.output,'w',ZIP_DEFLATED) as z:
            for path in sorted(bundle.rglob('*')):
                if path.is_file():
                    z.write(path,Path(NAME)/path.relative_to(bundle))
        with ZipFile(args.output) as z:
            assert z.testzip() is None
        checksum=sha256(args.output)
        args.output.with_suffix('.sha256').write_text(f'{checksum}  {args.output.name}\n',encoding='ascii')
        print(f'Built: {args.output}; {args.output.stat().st_size} bytes; SHA256={checksum}',flush=True)


if __name__=='__main__':
    main()
