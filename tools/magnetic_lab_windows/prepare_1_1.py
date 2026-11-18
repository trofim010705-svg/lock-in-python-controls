"""Reproduce the tested 1.1 sources from the published, hash-verified 1.0 ZIP."""
import hashlib
import json
from pathlib import Path
import re
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

root = Path(__file__).resolve().parents[2]
delta = json.loads(Path(__file__).with_name('release_1_1.json').read_text(encoding='utf-8'))
base = root / 'updates' / 'MagneticLab_v1.zip'
assert hashlib.sha256(base.read_bytes()).hexdigest() == delta['base_sha256'], 'Wrong base source ZIP'
with ZipFile(base) as z:
    assert z.testzip() is None
    files = {}
    for name in z.namelist():
        assert name.startswith('MagneticLab/') and '..' not in Path(name).parts
        if not name.endswith('/'):
            files[name[len('MagneticLab/'):]] = z.read(name).decode('utf-8')
for path, edits in delta['changes'].items():
    content = files[path]
    for index, edit in enumerate(edits):
        marker = edit.get('old', edit.get('start_marker'))
        if content.count(marker) != 1:
            raise RuntimeError(f'{path}: edit {index}, expected exactly one matching block, got {content.count(marker)}')
        if 'start_marker' in edit:
            content = content[:content.index(marker)] + edit['new']
        else:
            content = content.replace(marker, edit['new'], 1)
    files[path] = content
for path, content in delta['files'].items():
    assert path not in files
    files[path] = content
# Parse every Python file before anything is built or published.
for path, content in files.items():
    if path.endswith('.py'):
        compile(content, path, 'exec')
target = root / 'updates' / 'MagneticLab_v1_1.zip'
with ZipFile(target, 'w', ZIP_DEFLATED) as z:
    for path in sorted(files):
        item = ZipInfo('MagneticLab/' + path, date_time=(2026, 11, 3, 0, 0, 0))
        item.compress_type = ZIP_DEFLATED
        item.external_attr = 0o644 << 16
        z.writestr(item, files[path].encode('utf-8'))
with ZipFile(target) as z:
    assert z.testzip() is None
sha = hashlib.sha256(target.read_bytes()).hexdigest()
target.with_suffix('.sha256').write_text(f'{sha}  {target.name}\n', encoding='ascii')
builder = Path(__file__).with_name('build.py')
text = builder.read_text(encoding='utf-8')
text, count = re.subn(r"SOURCE_SHA256 = '[0-9a-f]+'", f"SOURCE_SHA256 = '{sha}'", text)
assert count == 1
builder.write_text(text, encoding='utf-8')
print(json.dumps({'source_zip':target.name, 'sha256':sha, 'files':len(files)}, ensure_ascii=True), flush=True)
