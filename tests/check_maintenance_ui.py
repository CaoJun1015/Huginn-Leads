"""Build the frontend first; NODE_PATH must contain an installed Playwright."""
import asyncio
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from unittest.mock import AsyncMock, patch

from fastapi.staticfiles import StaticFiles
import uvicorn
from test_lifecycle import LifecycleTests


def main():
    fixture = LifecycleTests()
    fixture.setUp()
    server = thread = None
    sock = socket.socket()
    try:
        fixture.seed()
        asyncio.run(fixture.db.set_setting('onboarding_seen', '1'))
        asyncio.run(fixture.service.cleanup())
        asyncio.run(fixture.service.backup())
        fixture.sql("CREATE TRIGGER fail_cleanup BEFORE INSERT ON audit WHEN NEW.action='cleanup' BEGIN SELECT RAISE(ABORT,'synthetic-private'); END")
        try:
            asyncio.run(fixture.service.cleanup())
        except Exception:
            pass
        sock.bind(('127.0.0.1', 0))
        origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
        object.__setattr__(fixture.settings, 'origin', origin)
        fixture.client.app.mount('/', StaticFiles(directory=Path(__file__).resolve().parents[1] / 'src/huginn_leads/static', html=True))
        server = uvicorn.Server(uvicorn.Config(fixture.client.app, log_level='error', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError('isolated server failed')
            time.sleep(0.05)
        javascript = r'''
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
(async () => {
  const browser = await chromium.launch({headless:true, channel:'msedge'});
  try {
    const page = await browser.newPage(); const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(process.env.TEST_ORIGIN);
    await page.getByLabel('密码', {exact:true}).fill('a-secure-local-password');
    await page.getByRole('button', {name:'登录',exact:true}).click();
    await page.locator('aside nav button').filter({hasText:'设置'}).click();
    const panel = page.locator('section.panel').filter({has:page.getByRole('heading',{name:'清理与备份结果'})});
    await panel.waitFor();
    const expected = process.env.TEST_STATE;
    await page.waitForFunction(state => [...document.querySelectorAll('h4')].filter(e => e.textContent.includes(state)).length === 2,
      expected === 'error' ? '最近操作失败' : '最近操作成功');
    const status = await page.evaluate(() => fetch('/api/status').then(r=>r.json()));
    for (const result of Object.values(status.maintenance)) {
      assert.equal(result.state, expected); assert.ok(result.last_success_at); assert.ok(result.last_failure_at);
    }
    assert.ok((await panel.innerText()).includes('PermissionError'));
    assert.ok(!(await panel.innerText()).includes('synthetic-private'));
    if (expected === 'error') {
      await page.getByRole('button',{name:'创建备份',exact:true}).click();
      await page.getByRole('status').filter({hasText:'备份失败'}).waitFor();
    }
    assert.deepEqual(errors, []);
    console.log(`PASS: maintenance UI ${expected}, timestamps and safe codes, no script errors`);
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        def browser(state):
            subprocess.run(['node', '-e', javascript], env={**os.environ, 'TEST_ORIGIN': origin, 'TEST_STATE': state}, timeout=45, check=True)
        with patch.object(fixture.service, '_backup', new=AsyncMock(side_effect=PermissionError('synthetic-private'))):
            try:
                asyncio.run(fixture.service.backup())
            except PermissionError:
                pass
            browser('error')
        fixture.sql('DROP TRIGGER fail_cleanup')
        asyncio.run(fixture.service.cleanup())
        asyncio.run(fixture.service.backup())
        browser('ok')
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(10)
        sock.close()
        fixture.tearDown()


if __name__ == '__main__':
    main()
