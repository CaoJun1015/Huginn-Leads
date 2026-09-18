from pathlib import Path
import os
import sys

ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(ROOT / 'src'))
DATA_ROOT = Path(os.environ.get('HUGINN_ROOT', ROOT)).resolve()
os.chdir(DATA_ROOT)
os.environ['HUGINN_ROOT'] = str(DATA_ROOT)


def main() -> int:
    if len(sys.argv) > 1:
        from huginn_leads.restore import main as restore_main
        if sys.argv[1] == 'restore':
            return restore_main(sys.argv[2:], DATA_ROOT / 'data/huginn-leads.db', DATA_ROOT / 'backups')
        print('Usage: HuginnLeads.exe [restore <backup.db>]')
        return 0 if sys.argv[1] in ('--help', '-h') else 2

    from huginn_leads.config import ensure_env
    if not (DATA_ROOT / '.env').exists():
        ensure_env(DATA_ROOT)
        print('[Huginn Leads] 已创建配置文件 .env', flush=True)

    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter
    nonebot.init()
    nonebot.get_driver().register_adapter(Adapter)
    from huginn_leads.runtime import install
    install()
    nonebot.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
