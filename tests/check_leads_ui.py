"""Build the frontend first; NODE_PATH must contain an installed Playwright."""
import asyncio
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

from fastapi.staticfiles import StaticFiles
import uvicorn

from test_lifecycle import LifecycleTests


def main():
    fixture = LifecycleTests()
    fixture.setUp()
    server = thread = None
    sock = socket.socket()
    try:
        _, task = fixture.seed()
        for index in range(49):
            filler = fixture.ingest(f'filler-{index}', text=f'synthetic-filler-{index}')
            fixture.manual(task, filler)
        first_message = fixture.ingest('first', text='synthetic-initial')
        first_lead = fixture.manual(task, first_message)
        fixture.follow(first_lead)
        slow_message = fixture.ingest('slow', text='synthetic-slow')
        fixture.manual(task, slow_message)
        refresh_message = fixture.ingest('refresh', text='synthetic-refresh')
        visible_message = fixture.ingest('visible', text='synthetic-visible')
        asyncio.run(fixture.db.set_setting('onboarding_seen', '1'))

        sock.bind(('127.0.0.1', 0))
        origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
        object.__setattr__(fixture.settings, 'origin', origin)
        fixture.client.app.mount('/', StaticFiles(
            directory=Path(__file__).resolve().parents[1] / 'src/huginn_leads/static', html=True,
        ))
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
  const browser = await chromium.launch({headless:true, channel:process.env.BROWSER_CHANNEL || 'msedge'});
  try {
    const page = await browser.newPage();
    const errors = []; let failNext = false;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/leads?*', async route => {
      const query = new URL(route.request().url()).searchParams.get('q');
      if (failNext) { failNext = false; await route.abort(); return; }
      if (query === 'synthetic-slow') await new Promise(resolve => setTimeout(resolve, 600));
      await route.continue();
    });
    await page.goto(process.env.TEST_ORIGIN);
    await page.getByLabel('密码', {exact:true}).fill('a-secure-local-password');
    await page.getByRole('button', {name:'登录', exact:true}).click();

    await page.locator('aside nav button').filter({hasText:'监控任务'}).click();
    await page.getByRole('button', {name:'新建手工任务'}).click();
    const taskDialog = page.locator('dialog[open]');
    await taskDialog.getByText('包含所选本地日期，到次日零点停止。', {exact:true}).waitFor();
    await taskDialog.getByText('仅记录，不参与匹配。', {exact:true}).waitFor();
    await taskDialog.getByRole('button', {name:'关闭'}).click();
    await page.locator('aside nav button').filter({hasText:'线索工作台'}).click();
    await page.getByText('synthetic-slow', {exact:true}).waitFor();
    await page.getByRole('button', {name:'加载更多'}).click();
    await page.getByText('synthetic-initial', {exact:true}).waitFor();
    await page.waitForFunction(() => document.querySelectorAll('tbody tr').length === 51);
    assert.equal(await page.locator('tbody tr').count(), 51);

    const search = page.getByRole('searchbox');
    await search.fill('synthetic-slow');
    await page.waitForTimeout(220);
    await search.fill('synthetic-initial');
    await page.getByText('synthetic-initial', {exact:true}).waitFor();
    await page.waitForTimeout(700);
    assert.equal(await page.locator('tbody tr').count(), 1, 'stale slow filter replaced the current result');

    await page.getByText('synthetic-initial', {exact:true}).click();
    const dialog = page.locator('dialog[open]');
    const note = dialog.getByLabel('人工备注');
    await note.fill('unsaved-browser-note');
    const auth = await page.evaluate(() => fetch('/api/auth/status').then(response => response.json()));
    async function addManual(messageId) {
      return page.evaluate(async ({task, messageId, csrf}) => {
        const response = await fetch(`/api/tasks/${task}/manual-leads`, {
          method:'POST', credentials:'same-origin',
          headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrf},
          body:JSON.stringify({message_id:messageId}),
        });
        if (!response.ok) throw new Error(await response.text());
      }, {task:process.env.TASK_ID, messageId, csrf:auth.csrf_token});
    }

    await addManual(process.env.REFRESH_MESSAGE);
    await page.getByRole('button', {name:'刷新线索'}).evaluate(button => button.click());
    await page.getByText('线索已刷新', {exact:true}).waitFor();
    assert.equal(await note.inputValue(), 'unsaved-browser-note');

    await addManual(process.env.VISIBLE_MESSAGE);
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.waitForTimeout(300);
    assert.equal(await note.inputValue(), 'unsaved-browser-note');
    await page.keyboard.press('Escape');
    await search.fill('');
    await page.getByText('synthetic-visible', {exact:true}).waitFor();

    failNext = true;
    await page.getByRole('button', {name:'刷新线索'}).click();
    await page.getByRole('alert').filter({hasText:'刷新失败'}).waitFor();
    await page.getByText('synthetic-visible', {exact:true}).waitFor();

    await page.evaluate(async csrf => {
      await fetch('/api/auth/logout', {method:'POST', credentials:'same-origin', headers:{'X-CSRF-Token':csrf}});
    }, auth.csrf_token);
    await page.getByRole('button', {name:'刷新线索'}).click();
    await page.getByLabel('密码', {exact:true}).waitFor();
    assert.deepEqual(errors, []);
    console.log('PASS: pagination, filter race, manual/visibility refresh, note preservation, failure retention, auth expiry');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
'''
        subprocess.run(
            ['node', '-e', javascript],
            env={
                **os.environ, 'TEST_ORIGIN': origin, 'TASK_ID': task,
                'REFRESH_MESSAGE': refresh_message, 'VISIBLE_MESSAGE': visible_message,
            },
            check=True, timeout=90,
        )
        assert fixture.rows('SELECT note FROM leads WHERE id=?', (first_lead,))[0]['note'] == 'synthetic-follow-up'
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(10)
        sock.close()
        fixture.tearDown()


if __name__ == '__main__':
    main()
