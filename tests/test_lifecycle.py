import asyncio
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_api
from huginn_leads.database import utcnow
from huginn_leads.rules import evaluate


class LifecycleTests(unittest.TestCase):
    # Reuse the existing isolated API/SQLite harness without inheriting its tests.
    setUp = test_api.ApiTests.setUp
    tearDown = test_api.ApiTests.tearDown
    authenticate = test_api.ApiTests.authenticate

    def sql(self, query, params=()):
        return asyncio.run(self.db.execute(query, params))

    def rows(self, query, params=()):
        return asyncio.run(self.db.fetch_all(query, params))

    def post(self, path, **kwargs):
        return self.client.post('/api' + path, headers=self.write_headers, **kwargs)

    def seed(self, account='10001', kind='group'):
        if not hasattr(self, 'write_headers'):
            self.authenticate()
        asyncio.run(self.service.connect_account(account, 'synthetic-account'))
        asyncio.run(self.service.refresh_sources(account, [{'group_id': 20001}] if kind == 'group' else [{'user_id': 20001}], kind))
        source = self.rows('SELECT id FROM sources WHERE account_id=? AND source_type=?', (account, kind))[0]['id']
        self.sql('UPDATE sources SET enabled=1 WHERE id=?', (source,))
        conditions = dict(direction='purchase', product='computer', aliases=['ThinkBook'], alias_mode='any', specs={}, price={}, quantity={}, trade={}, source_ids=[source])
        task = self.post('/tasks', json={'name': 'synthetic-task', 'conditions': conditions})
        self.assertEqual(task.status_code, 201, task.text)
        return source, task.json()['id']

    def ingest(self, native='m1', account='10001', kind='group', text='现货 ThinkBook 32G 1TB 4280元'):
        result = asyncio.run(self.service.ingest(dict(platform='qq', account_id=account, source_type=kind, peer_id='20001',
            native_message_id=native, sender_id='30001', sender_name='synthetic-sender',
            message_time='2020-01-02T03:04:05+00:00', text=text, segments=[])))
        self.assertTrue(result['saved'])
        return result['message_id']

    def drain(self):
        async def run():
            while job := await self.service._claim_job():
                await self.service._process_job(job)
        asyncio.run(run())

    def replay(self, task):
        now = datetime.now(timezone.utc)
        response = self.post(f'/tasks/{task}/replays', json={'start_at': (now - timedelta(days=1)).isoformat(), 'end_at': (now + timedelta(days=1)).isoformat()})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['id']

    def manual(self, task, message):
        response = self.post(f'/tasks/{task}/manual-leads', json={'message_id': message})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def follow(self, lead):
        version = self.rows('SELECT version FROM leads WHERE id=?', (lead,))[0]['version']
        response = self.client.patch(f'/api/leads/{lead}', headers=self.write_headers,
            json={'version': version, 'manual_status': 'contacted', 'note': 'synthetic-follow-up', 'manual_overrides': {'price': 123}, 'seen': True})
        self.assertEqual(response.status_code, 200, response.text)
        return {key: response.json()[key] for key in ('manual_status', 'note', 'manual_overrides', 'seen_at')}

    def assert_follow(self, lead, expected):
        actual = self.client.get(f'/api/leads/{lead}').json()
        self.assertEqual({key: actual[key] for key in expected}, expected)
        return actual

    def preview(self, **scope):
        response = self.post('/data/delete-preview', params=scope)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def delete_data(self, preview, **scope):
        return self.post('/data/delete', params=scope, json={'confirmation_token': preview['confirmation_token']})

    def task_preview(self, task, delete=True):
        response = self.post(f'/tasks/{task}/delete-preview', params={'delete_leads': delete})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def delete_task(self, task, preview, delete=True):
        return self.client.delete(f'/api/tasks/{task}', headers=self.write_headers,
            params={'delete_leads': delete, 'confirmation_token': preview['confirmation_token']})

    def dump(self):
        connection = sqlite3.connect(self.db.path)
        try:
            return list(connection.iterdump())
        finally:
            connection.close()

    def test_manual_nonmatch_repeated_replay_and_paused_completed_tasks(self):
        _, task = self.seed()
        message = self.ingest(text='synthetic-not-a-match')
        self.drain()
        lead = self.manual(task, message)
        expected = self.follow(lead)
        for status in ('active', 'active', 'paused', 'completed'):
            self.sql('UPDATE tasks SET status=? WHERE id=?', (status, task))
            self.replay(task)
            self.drain()
            self.assertEqual(self.manual(task, message), lead)
            actual = self.assert_follow(lead, expected)
            self.assertEqual((actual['origin'], actual['outcome'], actual['superseded_at']), ('manual', 'review', None))
            self.assertEqual(self.rows('SELECT status FROM tasks WHERE id=?', (task,))[0]['status'], status)
            self.assertEqual([r['id'] for r in self.client.get('/api/leads').json()['items']], [lead])
        self.assertEqual(len(self.rows('SELECT * FROM leads')), 1)

    def test_machine_same_changed_empty_results_preserve_human_fields(self):
        _, task = self.seed()
        message = self.ingest()
        self.drain()
        machine = self.rows("SELECT id FROM leads WHERE origin='machine'")[0]['id']
        expected = self.follow(machine)
        manual = self.manual(task, message)
        manual_expected = self.follow(manual)
        self.replay(task)
        self.drain()
        self.assert_follow(machine, expected)
        conditions = json.loads(self.rows('SELECT conditions_json FROM task_revisions')[0]['conditions_json'])
        result = evaluate('现货 ThinkBook 32G 1TB 4280元', conditions)[0]
        for results in ([dict(result, item_key='changed-product')], [], [result], [result]):
            self.replay(task)
            with patch('huginn_leads.service.evaluate', return_value=results):
                self.drain()
            self.assert_follow(machine, expected)
            self.assertIsNone(self.assert_follow(manual, manual_expected)['superseded_at'])
            visible = self.client.get('/api/leads').json()['items']
            self.assertEqual({r['item_key'] for r in visible if r['origin'] == 'machine'}, {r['item_key'] for r in results})
        self.assertEqual(len(self.rows("SELECT * FROM leads WHERE origin='manual'")), 1)

    def test_expired_evidence_trace_search_delete_and_retention(self):
        source, task = self.seed()
        message = self.ingest()
        self.drain()
        manual = self.manual(task, message)
        expected = self.follow(manual)
        before = self.client.get(f'/api/leads/{manual}').json()
        self.sql("UPDATE raw_messages SET expires_at='2000-01-01'")
        self.assertEqual(asyncio.run(self.service.cleanup())['messages'], 1)
        after = self.assert_follow(manual, expected)
        for key in ('platform', 'account_id', 'source_id', 'source_type', 'peer_id', 'source_name', 'native_message_id', 'message_time', 'received_at', 'sender_id', 'sender_name'):
            self.assertEqual(after[key], before[key], key)
            self.assertIsNotNone(after[key], key)
        self.assertIsNone(after['raw_text'])
        self.assertEqual(after['message_time'], '2020-01-02T03:04:05+00:00')
        found = self.client.get('/api/leads', params={'q': 'ThinkBook'}).json()['items']
        self.assertEqual(len(found), 2)
        preview = self.preview(source_id=source)
        self.assertEqual((preview['message_count'], preview['lead_count'], preview['evidence_count']), (0, 2, 2))
        self.assertEqual([r['task_id'] for r in preview['affected_tasks']], [task])
        response = self.delete_data(preview, source_id=source)
        self.assertEqual(response.json(), {'deleted_messages': 0, 'deleted_leads': 2})
        # Evidence expiration still clears manual follow-up at its own retention boundary.
        message = self.ingest('retention')
        self.manual(task, message)
        self.sql("UPDATE leads SET evidence_expires_at='2000-01-01'")
        self.assertEqual(asyncio.run(self.service.cleanup())['leads'], 1)

    def test_scope_account_type_and_shared_message_isolation(self):
        first, task1 = self.seed()
        _, task2 = self.seed()
        second, _ = self.seed('10002')
        contact, _ = self.seed(kind='contact')
        for account, kind in (('10001', 'group'), ('10002', 'group'), ('10001', 'contact')):
            self.ingest(account=account, kind=kind)
        self.drain()
        self.sql("UPDATE raw_messages SET expires_at='2000-01-01' WHERE source_id=?", (first,))
        asyncio.run(self.service.cleanup())
        preview = self.preview(source_id=first)
        self.assertEqual({r['task_id'] for r in preview['affected_tasks']}, {task1, task2})
        self.assertEqual(preview['lead_count'], 2)
        self.assertEqual(self.delete_data(preview, source_id=first).status_code, 200)
        self.assertEqual({r['source_id'] for r in self.rows('SELECT * FROM leads')}, {second, contact})
        self.assertEqual(len(self.rows('SELECT * FROM raw_messages')), 2)
        self.assertEqual(len(self.rows('SELECT * FROM tasks')), 4)
        self.assertEqual(self.rows('PRAGMA foreign_key_check'), [])

    def test_one_scope_deletes_live_and_expired_evidence_across_tasks(self):
        source, task = self.seed()
        _, other = self.seed()
        expired = self.ingest('expired')
        self.ingest('live')
        self.drain()
        self.manual(task, expired)
        self.sql("UPDATE raw_messages SET expires_at='2000-01-01' WHERE id=?", (expired,))
        asyncio.run(self.service.cleanup())
        preview = self.preview(source_id=source)
        self.assertEqual((preview['message_count'], preview['lead_count']), (1, 5))
        self.assertEqual({r['task_id'] for r in preview['affected_tasks']}, {task, other})
        self.assertEqual(self.delete_data(preview, source_id=source).json(), {'deleted_messages': 1, 'deleted_leads': 5})
        self.assertEqual(self.rows('SELECT * FROM leads'), [])
        self.assertEqual(self.rows('SELECT * FROM raw_messages'), [])
        self.assertEqual(self.rows('SELECT * FROM evaluations'), [])
        self.assertEqual(len(self.rows('SELECT * FROM tasks')), 2)
        self.assertEqual(self.rows('PRAGMA foreign_key_check'), [])

    def test_cutoff_only_combined_exclusive_boundary_and_unknown_history(self):
        source, task = self.seed()
        other, _ = self.seed('10002')
        for index, account, received in ((1, '10001', '2026-09-01T00:00:00+00:00'), (2, '10001', '2026-09-02T00:00:00+00:00'), (3, '10002', '2026-09-01T08:00:00+08:00')):
            message = self.ingest(str(index), account=account)
            self.sql('UPDATE raw_messages SET received_at=? WHERE id=?', (received, message))
        self.drain()
        # Legacy evidence with irrecoverable source and time is explicitly unknown.
        self.sql("UPDATE leads SET source_id=NULL, received_at=NULL, message_time=NULL WHERE source_id=?", (other,))
        self.sql("UPDATE raw_messages SET expires_at='2000-01-01' WHERE source_id=?", (other,))
        asyncio.run(self.service.cleanup())
        scope = {'source_id': source, 'before': '2026-09-02T08:00:00+08:00'}
        preview = self.preview(**scope)
        self.assertEqual((preview['message_count'], preview['lead_count']), (1, 1))
        self.assertEqual(self.delete_data(preview, **scope).json(), {'deleted_messages': 1, 'deleted_leads': 1})
        preview = self.preview(before='2026-09-03T00:00:00Z')
        self.assertEqual(self.delete_data(preview, before='2026-09-03T00:00:00+00:00').json(), {'deleted_messages': 1, 'deleted_leads': 1})
        self.assertEqual(len(self.rows('SELECT * FROM leads')), 1)
        self.assertIsNone(self.client.get('/api/leads').json()['items'][0]['message_time'])

    def test_confirmation_scope_empty_invalid_expired_and_reused_tokens(self):
        source, _ = self.seed()
        self.ingest()
        for scope in ({}, {'source_id': 0}, {'before': ''}, {'before': 'nonsense'}, {'before': '2026-09-01'}):
            self.assertEqual(self.post('/data/delete-preview', params=scope).status_code, 400)
            self.assertEqual(self.post('/data/delete', params=scope, json={'confirmation_token': 'invalid'}).status_code, 400)
        preview = self.preview(source_id=source)
        self.assertEqual(self.delete_data(preview, source_id=source + 1).status_code, 400)
        preview = self.preview(source_id=source)
        class Future(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(minutes=11)
        with patch('huginn_leads.api.datetime', Future):
            self.assertEqual(self.delete_data(preview, source_id=source).status_code, 400)
        preview = self.preview(source_id=source)
        self.assertEqual(self.delete_data(preview, source_id=source).status_code, 200)
        self.assertEqual(self.delete_data(preview, source_id=source).status_code, 400)

    def test_scope_delete_failure_rolls_back_leads_jobs_and_raw(self):
        source, task = self.seed()
        message = self.ingest()
        self.manual(task, message)
        self.sql("CREATE TRIGGER fail_delete BEFORE DELETE ON raw_messages BEGIN SELECT RAISE(ABORT, 'synthetic-private'); END")
        before = self.dump()
        response = self.delete_data(self.preview(source_id=source), source_id=source)
        self.assertEqual((response.status_code, response.json()['code']), (409, 'delete_failed'))
        self.assertNotIn('synthetic-private', response.text)
        self.assertEqual(self.dump(), before)
        self.sql('DROP TRIGGER fail_delete')
        self.assertEqual(self.delete_data(self.preview(source_id=source), source_id=source).status_code, 200)

    def test_task_delete_with_replays_jobs_shared_raw_and_external_link(self):
        source, task = self.seed()
        _, other = self.seed()
        self.ingest()
        self.drain()
        self.replay(task)
        self.drain()  # Completed replay.
        self.replay(task)  # Pending replay reuses the job.
        self.sql("INSERT INTO external_sources(id,name,database_path,query_text,key_column,template_id,template_version,field_mapping_json,name_fields_json,alias_fields_json,created_at,updated_at) VALUES ('ext','ext','unused','SELECT 1','id','supply-demand',1,'{}','[]','[]','now','now')")
        self.sql("INSERT INTO external_records(id,external_source_id,external_key,template_id,template_version,display_name,aliases_json,values_json,content_hash,task_id,first_imported_at,last_seen_at,updated_at) VALUES ('record','ext','key','supply-demand',1,'name','[]','{}','hash',?,'now','now','now')", (task,))
        preview = self.task_preview(task)
        second_token = self.task_preview(task)
        self.assertEqual(preview['lead_count'], 1)
        self.assertEqual(self.delete_task(task, preview).json(), {'deleted': True})
        self.assertEqual(self.delete_task(task, preview).status_code, 400)
        self.assertEqual(self.delete_task(task, second_token).status_code, 404)
        self.assertEqual(self.post(f'/tasks/{task}/delete-preview').status_code, 404)
        for table in ('leads', 'evaluations', 'processing_jobs', 'replays', 'task_revisions', 'task_revision_sources'):
            self.assertEqual(self.rows(f'SELECT * FROM {table} WHERE task_id=?', (task,)), [], table)
        self.assertEqual(len(self.rows('SELECT * FROM raw_messages')), 1)
        self.assertEqual(self.rows('SELECT task_id FROM leads')[0]['task_id'], other)
        self.assertIsNone(self.rows('SELECT task_id FROM external_records')[0]['task_id'])
        self.assertEqual(self.rows('PRAGMA foreign_key_check'), [])
        # Task with no replay also deletes normally.
        self.assertEqual(self.delete_task(other, self.task_preview(other)).status_code, 200)

    def test_archive_preserves_leads_and_cancels_work(self):
        _, task = self.seed()
        self.ingest()
        self.drain()
        self.replay(task)
        self.assertEqual(self.delete_task(task, self.task_preview(task, False), False).json(), {'archived': True})
        self.drain()
        self.assertEqual(self.rows('SELECT status FROM tasks')[0]['status'], 'archived')
        self.assertEqual(len(self.rows('SELECT * FROM leads')), 1)
        self.assertEqual(self.rows('SELECT status FROM processing_jobs')[0]['status'], 'cancelled')

    def test_task_delete_failure_rolls_back_all_relationships(self):
        _, task = self.seed()
        self.ingest()
        self.drain()
        self.replay(task)
        self.sql("CREATE TRIGGER fail_delete BEFORE DELETE ON tasks BEGIN SELECT RAISE(ABORT, 'synthetic-private'); END")
        before = self.dump()
        response = self.delete_task(task, self.task_preview(task))
        self.assertEqual((response.status_code, response.json()['code']), (409, 'delete_failed'))
        self.assertEqual(self.dump(), before)
        self.sql('DROP TRIGGER fail_delete')
        self.assertEqual(self.delete_task(task, self.task_preview(task)).status_code, 200)

    def test_worker_cannot_write_after_task_delete_raw_delete_or_expiry(self):
        source, task = self.seed()
        for operation in ('expiry', 'scope', 'task'):
            with self.subTest(operation=operation):
                self.ingest(operation)
                job = asyncio.run(self.service._claim_job())
                original_write = self.db.write
                async def intercept(callback):
                    with patch.object(self.db, 'write', original_write):
                        if operation == 'task':
                            response = self.delete_task(task, self.task_preview(task))
                            self.assertEqual(response.status_code, 200)
                        elif operation == 'scope':
                            response = self.delete_data(self.preview(source_id=source), source_id=source)
                            self.assertEqual(response.status_code, 200)
                        else:
                            await self.db.execute("UPDATE raw_messages SET expires_at='2000-01-01'")
                            await self.service.cleanup()
                    return await original_write(callback)
                with patch.object(self.db, 'write', intercept):
                    asyncio.run(self.service._process_job(job))
                self.assertEqual(self.rows('SELECT * FROM leads'), [])
                self.assertEqual(self.rows('SELECT * FROM evaluations'), [])
                self.assertEqual(self.rows('PRAGMA foreign_key_check'), [])

    def test_manual_insert_and_snapshot_failure_roll_back_together(self):
        _, task = self.seed()
        message = self.ingest()
        self.sql("CREATE TRIGGER fail_snapshot BEFORE UPDATE OF received_at ON leads BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        before = self.dump()
        with self.assertRaises(sqlite3.IntegrityError):
            self.manual(task, message)
        self.assertEqual(self.dump(), before)


if __name__ == '__main__':
    unittest.main()
