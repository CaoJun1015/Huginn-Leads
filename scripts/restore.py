from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from huginn_leads.restore import integrity, main as restore_main

DATABASE = ROOT / 'data' / 'huginn-leads.db'
BACKUPS = ROOT / 'backups'


def main() -> int:
    return restore_main(sys.argv[1:], DATABASE, BACKUPS)


if __name__ == '__main__':
    raise SystemExit(main())
