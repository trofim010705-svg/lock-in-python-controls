"""Frozen entry point: handle multiprocessing before importing any GUI code."""
import json
import multiprocessing
from pathlib import Path
import sys
import traceback


if __name__ == '__main__':
    multiprocessing.freeze_support()
    try:
        if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
            from selftest import run
            sys.exit(run(sys.argv[2]))
        from app import main
        sys.exit(main())
    except Exception:
        if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
            Path(sys.argv[2]).write_text(json.dumps({'ok': False, 'error': traceback.format_exc()}), encoding='utf-8')
            sys.exit(1)
        raise
