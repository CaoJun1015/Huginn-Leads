"""Validate an actual Windows ZIP in an isolated Chinese/space directory; never use production data."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile

import httpx
from huginn_leads.config import Settings
from huginn_leads.database import Database, utcnow
from huginn_leads.restore import MIGRATIONS, validate_backup
from huginn_leads.service import Workbench
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_database import MigrationTests


def main(archive: Path):
    report = {'archive': archive.name, 'checks': [], 'clean_windows': 'pending'}
    with zipfile.ZipFile(archive) as zipped:
        names = zipped.namelist()
        root_name = names[0].split('/')[0]
        for required in ('HuginnLeads.exe', 'start.cmd', 'restore.cmd', '.env.example', 'LICENSE', 'THIRD_PARTY.md', 'Windows测试包使用与恢复.md'):
            assert f'{root_name}/{required}' in names, required
        assert not any(Path(name).name == '.env' or Path(name).suffix in ('.db', '.log') or name.endswith(('-wal', '-shm')) for name in names)
        report['file_count'] = sum(not name.endswith('/') for name in names)
        with tempfile.TemporaryDirectory(prefix='发布包 恢复 中文 ') as directory:
            zipped.extractall(directory)
            bundle = Path(directory) / root_name
            exe = bundle / 'HuginnLeads.exe'
            dbpath = bundle / 'data/huginn-leads.db'
            env = {key: value for key, value in os.environ.items() if key not in ('HUGINN_ROOT', 'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
            env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
            env['PYTHONIOENCODING'] = 'utf-8'
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                env['PORT'] = str(sock.getsockname()[1])
            env['HOST'] = '127.0.0.1'
            env['LOG_LEVEL'] = 'INFO'
            origin = 'http://127.0.0.1:' + env['PORT']
            process = log = client = None
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            def command(args, expected=0):
                result = subprocess.run([str(exe), *args], cwd=bundle, env=env, capture_output=True, timeout=25)
                assert result.returncode == expected, result.stderr.decode('utf-8', errors='replace')
                return result
            def start():
                nonlocal process, log, client
                log = (bundle / 'runtime.log').open('wb')
                process = subprocess.Popen([str(exe)], cwd=bundle, env=env, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
                client = httpx.Client(base_url=origin, headers={'Origin': origin}, timeout=5)
                end = time.monotonic() + 30
                while time.monotonic() < end:
                    if process.poll() is not None:
                        content = (bundle / 'runtime.log').read_text(encoding='utf-8', errors='replace')
                        content = re.sub(r'initialization code: .*', 'initialization code: [redacted]', content)
                        raise AssertionError('packaged startup failed: ' + content[-3000:])
                    try:
                        response = client.get('/api/auth/status')
                        if response.status_code == 200:
                            return response.json()
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                raise AssertionError('packaged startup timed out')
            def stop(crash=False):
                nonlocal process, log, client
                if process and process.poll() is None:
                    if crash:
                        process.kill()
                    else:
                        helper = 'import ctypes,sys; k=ctypes.windll.kernel32; k.FreeConsole(); assert k.AttachConsole(int(sys.argv[1])); assert k.GenerateConsoleCtrlEvent(1,int(sys.argv[1]))'
                        subprocess.run([sys.executable, '-c', helper, str(process.pid)], check=True, timeout=10)
                    process.wait(timeout=20)
                if client:
                    client.close()
                if log:
                    log.close()
                process = log = client = None
            def login(initial=False):
                if initial:
                    text = (bundle / 'runtime.log').read_text(encoding='utf-8', errors='replace')
                    code = re.search(r'initialization code: (\S+)', text)[1]
                    response = client.post('/api/auth/setup', json={'code': code, 'password': 'synthetic-package-password'})
                else:
                    response = client.post('/api/auth/login', json={'password': 'synthetic-package-password'})
                assert response.status_code == 200
                client.headers['X-CSRF-Token'] = response.json()['csrf_token']
            def query(sql, params=()):
                with closing(sqlite3.connect(dbpath)) as connection:
                    return connection.execute(sql, params).fetchall()
            try:
                command(['--help'])
                command(['restore'], 2)
                command(['invalid'], 2)
                assert not (bundle / '.env').exists()
                status = start()
                login(initial=not status['initialized'])
                assert client.get('/').status_code == 200
                settings = Settings(bundle, bundle / 'data', bundle / 'backups', dbpath, bundle / '_internal/huginn_leads/static', origin)
                db = Database(dbpath, MIGRATIONS)
                service = Workbench(db, settings)
                asyncio.run(service.connect_account('10001', 'synthetic-account'))
                asyncio.run(service.refresh_sources('10001', [{'group_id': 20001, 'group_name': 'synthetic-group'}]))
                source = client.get('/api/sources').json()['items'][0]
                assert client.patch(f"/api/sources/{source['id']}", json={'version': source['version'], 'enabled': True}).status_code == 200
                conditions = {'direction':'purchase', 'product':'synthetic', 'aliases':['ThinkBook'], 'source_ids':[source['id']]}
                response = client.post('/api/tasks', json={'name':'synthetic-package-task','conditions':conditions})
                assert response.status_code == 201, response.text
                task = response.json()['id']
                asyncio.run(service.touch_account('10001'))
                result = asyncio.run(service.ingest({'platform':'qq', 'account_id':'10001', 'group_id':'20001', 'native_message_id':'synthetic-1', 'sender_id':'30001', 'sender_name':'synthetic-publisher', 'message_time':utcnow(), 'text':'synthetic-manual-evidence', 'segments':[]}))
                response = client.post(f'/api/tasks/{task}/manual-leads',json={'message_id':result['message_id']})
                assert response.status_code == 201
                lead = response.json()['id']
                current = client.get(f'/api/leads/{lead}').json()
                response = client.patch(f'/api/leads/{lead}',json={'version':current['version'],'manual_status':'contacted','note':'before-restore','manual_overrides':{'price':123},'seen':True})
                assert response.status_code == 200
                backup = client.post('/api/backups')
                assert backup.status_code == 201, backup.text
                snapshot = bundle / 'backups' / backup.json()['name']
                command(['restore', str(snapshot)], 1)
                assert query('SELECT note FROM leads WHERE id=?',(lead,))[0][0] == 'before-restore'
                report['checks'].append('startup/API data/manual fields/backup/running restore rejected')
                stop()
                assert query('SELECT connected FROM accounts')[0][0] == 0
                assert query("SELECT reason FROM connection_gaps WHERE ended_at IS NULL")[0][0] == 'application_stopped'
                with closing(sqlite3.connect(dbpath)) as connection:
                    connection.execute('UPDATE leads SET note=? WHERE id=?', ('after-backup',lead)); connection.commit()
                for name, content in (('corrupt.db',b'bad SQLite'),):
                    bad = bundle / name; bad.write_bytes(content)
                    command(['restore',str(bad)],1)
                unrelated = bundle / 'unrelated.db'
                with closing(sqlite3.connect(unrelated)) as connection:
                    connection.execute('CREATE TABLE unrelated(value)')
                command(['restore',str(unrelated)],1)
                assert query('SELECT note FROM leads WHERE id=?',(lead,))[0][0] == 'after-backup'
                # Exercise the distributed batch wrapper, with no Python/Node on PATH.
                result = subprocess.run([str(Path(os.environ['SystemRoot']) / 'System32/cmd.exe'), '/d', '/c', 'restore.cmd', str(snapshot)], cwd=bundle, env=env, capture_output=True, timeout=25)
                assert result.returncode == 0, result.stderr.decode(errors='replace')
                emergency = sorted((bundle / 'backups').glob('before-restore-*.db'))[-1]
                with closing(sqlite3.connect(emergency)) as connection:
                    assert connection.execute('SELECT note FROM leads WHERE id=?',(lead,)).fetchone()[0] == 'after-backup'
                assert not Path(str(dbpath)+'-wal').exists() and not Path(str(dbpath)+'-shm').exists()
                start(); login()
                restored = client.get(f'/api/leads/{lead}').json()
                assert (restored['note'],restored['manual_status'],restored['manual_overrides']) == ('before-restore','contacted',{'price':123})
                assert restored['seen_at']
                assert client.get('/api/status').json()['accounts_connected'] == 0
                assert len(client.get('/api/tasks').json()['items']) == 1
                assert query('PRAGMA integrity_check') == [('ok',)] and query('PRAGMA foreign_key_check') == []
                report['checks'].append('graceful stop/invalid backups refused/batch restore/emergency copy/restart fields and integrity')
                asyncio.run(service.connect_account('10001','synthetic-account'))
                stop(crash=True)
                assert query('SELECT connected FROM accounts')[0][0] == 1
                start(); login()
                assert client.get('/api/status').json()['accounts_connected'] == 0
                assert query("SELECT reason FROM connection_gaps WHERE ended_at IS NULL")[0][0] == 'restart_time_unknown'
                stop()
                report['checks'].append('forced termination/OS lock released/stale online reset/uncertain gap')
                # Real frozen restore of a supported v5 backup, upgraded by next startup.
                scripts = bundle / 'old-migrations'; scripts.mkdir()
                for path in sorted(MIGRATIONS.glob('*.sql'))[:1]: shutil.copyfile(path,scripts/path.name)
                old_path = bundle / '旧版 v5.db'
                old = Database(old_path,scripts)
                asyncio.run(old.migrate()); asyncio.run(MigrationTests().seed_v1(old))
                for path in sorted(MIGRATIONS.glob('*.sql'))[1:5]: shutil.copyfile(path,scripts/path.name)
                asyncio.run(old.migrate())
                password = query("SELECT value FROM settings WHERE key='admin_password'")[0][0]
                asyncio.run(old.set_setting('admin_password',password))
                command(['restore',str(old_path)])
                assert validate_backup(dbpath) == 5
                start(); login()
                assert validate_backup(dbpath) == 6
                old_lead = client.get('/api/leads/l').json()
                assert (old_lead['origin'],old_lead['note'],old_lead['manual_status']) == ('manual','follow-up','contacted')
                assert query('PRAGMA integrity_check') == [('ok',)] and query('PRAGMA foreign_key_check') == []
                report['restored_counts'] = {table:query(f'SELECT COUNT(*) FROM {table}')[0][0] for table in ('tasks','leads','raw_messages')}
                report['schema_version'] = 6
                stop()
                report['checks'].append('v5 frozen restore/v6 startup migration/manual evidence preserved')
                report['path_environment'] = 'Chinese and spaces; child PATH contains only Windows System32'
            finally:
                if process and process.poll() is None:
                    process.kill(); process.wait(timeout=10)
                if client: client.close()
                if log: log.close()
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve())
