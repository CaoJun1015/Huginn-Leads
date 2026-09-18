"""Optional real-browser check: build frontend, set NODE_PATH to Playwright, then run this file."""
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
        message = fixture.ingest(text='synthetic-evidence-known')
        known = fixture.manual(task, message)
        fixture.follow(known)
        fixture.replay(task)
        fixture.drain()
        message = fixture.ingest('unknown', text='synthetic-evidence-unknown')
        unknown = fixture.manual(task, message)
        fixture.sql("UPDATE raw_messages SET expires_at='2000-01-01'")
        asyncio.run(fixture.service.cleanup())
        fixture.sql("UPDATE leads SET platform=NULL, account_id=NULL, source_id=NULL, source_type=NULL, peer_id=NULL, source_name=NULL, native_message_id=NULL, message_time=NULL, received_at=NULL WHERE id=?", (unknown,))
        asyncio.run(fixture.db.set_setting('onboarding_seen', '1'))
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        # The isolated fixture's API closure holds this settings instance.
        object.__setattr__(fixture.settings, 'origin', origin)
        static = Path(__file__).resolve().parents[1] / 'src/huginn_leads/static'
        fixture.client.app.mount('/', StaticFiles(directory=static, html=True), name='ui')
        server = uvicorn.Server(uvicorn.Config(fixture.client.app, log_level='error', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError('isolated UI server did not start')
            time.sleep(0.05)
        javascript = r'''
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({headless: true, channel: process.env.BROWSER_CHANNEL || 'msedge'});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(process.env.TEST_ORIGIN);
    await page.getByLabel('密码', {exact:true}).fill('a-secure-local-password');
    await page.getByRole('button', {name:'登录', exact:true}).click();
    const search = page.getByRole('searchbox');
    await search.fill('synthetic-evidence-known');
    const row = page.locator('tbody tr').filter({hasText:'synthetic-evidence-known'});
    await row.waitFor();
    await row.focus();
    await page.keyboard.press('Enter');
    const dialog = page.locator('dialog[open]');
    await dialog.waitFor();
    const text = await dialog.innerText();
    for (const value of ['synthetic-sender', '30001', '20001', '账号 10001', '消息 m1', '2020', '接收：', 'synthetic-evidence-known']) {
      assert.ok(text.includes(value), `missing provenance: ${value}`);
    }
    assert.equal(await dialog.getByLabel('人工备注').inputValue(), 'synthetic-follow-up');
    await dialog.getByLabel('人工备注').fill('browser-follow-up');
    await dialog.getByRole('button', {name:'标为已联系'}).click();
    await page.getByText('线索记录已保存', {exact:true}).waitFor();
    await page.keyboard.press('Escape');
    await search.fill('synthetic-evidence-unknown');
    const unknown = page.locator('tbody tr').filter({hasText:'synthetic-evidence-unknown'});
    await unknown.waitFor();
    assert.ok((await unknown.innerText()).includes('消息时间未知'));
    assert.ok((await unknown.innerText()).includes('来源未知'));
    await unknown.click();
    await dialog.waitFor();
    const unknownText = await dialog.innerText();
    for (const value of ['来源未知', '账号未知', '消息标识未知', '消息时间未知', '接收：未知']) {
      assert.ok(unknownText.includes(value), `missing unknown marker: ${value}`);
    }
    assert.deepEqual(errors, []);
    console.log('PASS: expired evidence search, keyboard detail, provenance, note save, unknown history; no browser errors');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
'''
        subprocess.run(['node', '-e', javascript], env={**os.environ, 'TEST_ORIGIN': origin}, check=True, timeout=60)
        assert fixture.rows('SELECT note FROM leads WHERE id=?', (known,))[0]['note'] == 'browser-follow-up'
        assert fixture.rows('SELECT * FROM raw_messages') == []
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=10)
        sock.close()
        fixture.tearDown()


if __name__ == '__main__':
    main()
