"""Native Windows entry point with a structured self-test failure report."""
import json
from pathlib import Path
import sys
import traceback

if __name__ == '__main__':
    try:
        from app import main
        sys.exit(main())
    except Exception:
        if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
            report=Path(sys.argv[2])
            report.parent.mkdir(parents=True,exist_ok=True)
            report.write_text(json.dumps({'ok':False,'error':traceback.format_exc()}),encoding='utf-8')
            sys.exit(1)
        raise
