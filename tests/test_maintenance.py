import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import test_service
from huginn_leads.database import utcnow
from huginn_leads.service import Workbench


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_service.ServiceTests.asyncSetUp
    create_task = test_service.ServiceTests.create_task
    message = test_service.ServiceTests.message

    async def asyncTearDown(self):
        await self.service.stop()
        self.temp.cleanup()

    async def start_quiet(self):
        with patch.object(self.service, '_worker_loop', self.service.stop_event.wait), patch.object(self.service, '_maintenance_loop', self.service.stop_event.wait):
            await self.service.start()

    async def test_restart_resets_stale_accounts_and_marks_time_unknown(self):
        await self.service.connect_account('10002', 'synthetic-second')
        await self.db.execute("UPDATE accounts SET last_seen_at='2000-01-01'")
        before = utcnow()
        await self.start_quiet()
        status = await self.service.status()
        self.assertEqual(status['accounts_connected'], 0)
        self.assertIsNone(status['last_received_at'])
        gaps = await self.db.fetch_all('SELECT * FROM connection_gaps')
        self.assertEqual(len(gaps), 2)
        self.assertTrue(all(g['started_at'] >= before and g['reason'] == 'restart_time_unknown' for g in gaps))
        await self.service.connect_account('10001', 'synthetic')
        self.assertEqual((await self.service.status())['accounts_connected'], 1)
        self.assertIsNotNone((await self.db.fetch_one("SELECT ended_at FROM connection_gaps WHERE account_id='10001'"))['ended_at'])
        self.assertIsNone((await self.db.fetch_one("SELECT ended_at FROM connection_gaps WHERE account_id='10002'"))['ended_at'])

    async def test_normal_stop_and_one_account_disconnect_preserve_other(self):
        await self.service.connect_account('10002', 'synthetic-second')
        await self.service.disconnect_account('10001')
        self.assertEqual((await self.service.status())['accounts_connected'], 1)
        await self.service.stop()
        self.assertEqual((await self.service.status())['accounts_connected'], 0)
        self.assertEqual((await self.db.fetch_one("SELECT reason FROM connection_gaps WHERE account_id='10002'"))['reason'], 'application_stopped')
        await self.start_quiet()
        self.assertEqual((await self.db.fetch_one('SELECT COUNT(*) n FROM connection_gaps'))['n'], 2)
        await self.service.connect_account('10002', 'synthetic-second')
        self.assertEqual((await self.service.status())['accounts_connected'], 1)

    async def test_receive_save_times_survive_cleanup_and_do_not_mean_login(self):
        self.assertIsNone((await self.service.status())['last_received_at'])
        await self.service.touch_account('10001')
        await self.service.ingest(self.message())
        before = await self.service.status()
        self.assertIsNotNone(before['last_received_at'])
        self.assertIsNotNone(before['last_saved_at'])
        await self.db.execute("UPDATE raw_messages SET expires_at='2000-01-01'")
        await self.service.cleanup()
        after = await self.service.status()
        self.assertEqual(after['raw_messages'], 0)
        self.assertEqual((after['last_received_at'], after['last_saved_at']), (before['last_received_at'], before['last_saved_at']))

    async def test_failure_is_visible_persisted_bounded_and_recovers(self):
        await self.service.cleanup()
        first = await self.service.backup()
        second = await self.service.backup()
        self.assertNotEqual(first, second)
        await self.db.execute("CREATE TRIGGER fail_cleanup BEFORE INSERT ON audit WHEN NEW.action='cleanup' BEGIN SELECT RAISE(ABORT, 'synthetic-private'); END")
        for _ in range(3):
            with self.assertRaises(Exception):
                await self.service.cleanup()
        with patch.object(self.service, '_backup', new=AsyncMock(side_effect=PermissionError('synthetic-private'))):
            with self.assertRaises(PermissionError):
                await self.service.backup()
        status = (await self.service.status())['maintenance']
        for result in status.values():
            self.assertEqual(result['state'], 'error')
            self.assertIsNotNone(result['last_success_at'])
            self.assertIsNotNone(result['last_failure_at'])
        self.assertEqual(status['backup']['last_failure_code'], 'PermissionError')
        self.assertNotIn('synthetic-private', json.dumps(status))
        self.assertEqual((await self.db.fetch_one('SELECT COUNT(*) n FROM diagnostics'))['n'], 0)
        reloaded = Workbench(self.db, self.settings)
        self.assertEqual((await reloaded.status())['maintenance'], status)
        await self.db.execute('DROP TRIGGER fail_cleanup')
        await self.service.cleanup()
        await self.service.backup()
        for result in (await self.service.status())['maintenance'].values():
            self.assertEqual(result['state'], 'ok')
            self.assertIsNotNone(result['last_failure_at'])

    async def test_loop_runs_backup_despite_cleanup_failure_and_retries_after_60_seconds(self):
        delays = []
        async def stop_after_wait(seconds):
            delays.append(seconds)
            self.service.stop_event.set()
        with patch.object(self.service, '_cleanup', new=AsyncMock(side_effect=OSError('synthetic'))), patch.object(self.service, '_wait', stop_after_wait):
            await self.service._maintenance_loop()
        self.assertEqual(delays, [60])
        results = (await self.service.status())['maintenance']
        self.assertEqual(results['cleanup']['state'], 'error')
        self.assertEqual(results['backup']['state'], 'ok')
        self.assertEqual(len(list(self.settings.backup_dir.glob('huginn-leads-*.db'))), 1)

    async def test_status_record_failure_retains_in_memory_result_then_recovers(self):
        with patch.object(self.db, 'set_setting', new=AsyncMock(side_effect=OSError('synthetic'))):
            await self.service._record_maintenance('backup', PermissionError('private'))
        self.assertTrue((await self.service.status())['maintenance']['backup']['status_storage_error'])
        await self.service.backup()
        self.assertNotIn('status_storage_error', json.loads(await self.db.get_setting('maintenance_backup')))

    async def test_shutdown_waits_for_inflight_operation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            await release.wait()
        with patch.object(self.service, '_cleanup', slow):
            self.service.maintenance_task = asyncio.create_task(self.service.cleanup())
            await entered.wait()
            stopping = asyncio.create_task(self.service.stop())
            await asyncio.sleep(0)
            self.assertFalse(stopping.done())
            release.set()
            await stopping
        self.assertEqual((await self.service.status())['maintenance']['cleanup']['state'], 'ok')


if __name__ == '__main__':
    unittest.main()
