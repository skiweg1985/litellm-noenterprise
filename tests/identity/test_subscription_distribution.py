"""Distributed subscription models through native CRUD, inference and log APIs."""
import concurrent.futures
import json
import os
import time
import unittest
import uuid

import test_subscription_logs as log_tests
import test_subscription_models as model_tests


class SubscriptionDistributionTests(log_tests.SubscriptionLogTests):
    def create_distribution(self, accounts):
        name = 'subscription-distribution-' + uuid.uuid4().hex[:8]
        response = self.client.post('/model/new', json={
            'model_name': name,
            'litellm_params': {'model': 'chatgpt/gpt-5.4'},
            'model_info': {'subscription_assignment': {
                'mode': 'distribution',
                'subscription_ids': [account['subscription_id'] for account in accounts],
            }},
        })
        if response.status_code == 200:
            self.models.append(response.json()['model_id'])
        return name, response

    def completion(self, name, **kwargs):
        return self.client.post('/v1/chat/completions', json={
            'model': name,
            'messages': [{'role': 'user', 'content': 'Distribution ' + uuid.uuid4().hex}],
            **kwargs,
        })

    def setUp(self):
        super().setUp()
        self.control(status_by_identity={}, retry_after_by_identity={}, models_by_identity={}, stream_error_at=3)

    def burst(self, name, count, expected):
        self.control(clear=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.completion(name), range(count)))
        self.assertTrue(all(response.status_code == 200 for response in responses),
                        [(response.status_code, response.text[:200]) for response in responses if response.status_code != 200])
        self.assertEqual(set(self.control()['seen']), set(expected))
        self.assertEqual(len(self.control()['seen']), count)

    def test_disconnect_recovery_retirement_and_membership_edit_preserve_isolation(self):
        import time
        first, first_identity = self.connect('LifecycleFirst')
        second, second_identity = self.connect('LifecycleSecond')
        third, third_identity = self.connect('LifecycleThird')
        name, created = self.create_distribution([first, second])
        self.assertEqual(created.status_code, 200, created.text)
        model_id = created.json()['model_id']
        unrelated, fixed = self.create(first)
        self.assertEqual(fixed.status_code, 200, fixed.text)
        self.client.post('/chatgpt/api/accounts/' + first['account'] + '/logout').raise_for_status()
        self.burst(name, 8, [second_identity])
        self.control(identity=first_identity)
        self.client.post('/chatgpt/api/accounts/' + first['account'] + '/login').raise_for_status()
        for _ in range(100):
            account = next(row for row in self.client.get('/chatgpt/api/accounts').json()['accounts']
                           if row['subscription_id'] == first['subscription_id'])
            if account['token_valid']:
                break
            time.sleep(.1)
        else:
            self.fail('Recovered subscription did not reconnect')
        self.burst(name, 32, [first_identity, second_identity])
        self.client.post('/chatgpt/api/accounts/' + second['account'] + '/retire').raise_for_status()
        self.burst(name, 8, [first_identity])
        changed = self.client.patch('/model/' + model_id + '/update', json={
            'model_info': {'subscription_assignment': {'mode': 'distribution', 'subscription_ids': [third['subscription_id']]}}
        })
        self.assertEqual(changed.status_code, 200, changed.text)
        self.burst(name, 8, [third_identity])
        self.burst(unrelated, 2, [first_identity])
        stored = [row for row in self.client.get('/v1/model/info').json()['data'] if row['model_name'] == name]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['model_info']['id'], model_id)
        self.assertEqual(stored[0]['model_info']['subscription_assignment']['mode'], 'distribution')
        self.assertEqual(stored[0]['model_info']['subscription_assignment']['subscription_ids'], [third['subscription_id']])

    def test_every_member_catalog_is_validated_before_create_or_membership_edit(self):
        first, first_identity = self.connect('CatalogDistributionFirst')
        second, second_identity = self.connect('CatalogDistributionSecond')
        self.control(models_by_identity={second_identity: []})
        denied_name, rejected = self.create_distribution([first, second])
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertFalse(any(row['model_name'] == denied_name
            for row in self.client.get('/v1/model/info').json()['data']))
        name, created = self.create_distribution([first])
        self.assertEqual(created.status_code, 200, created.text)
        model_id = created.json()['model_id']
        rejected = self.client.patch('/model/' + model_id + '/update', json={
            'model_info': {'subscription_assignment': {'mode': 'distribution',
                'subscription_ids': [first['subscription_id'], second['subscription_id']]}}
        })
        self.assertEqual(rejected.status_code, 400, rejected.text)
        stored = next(row for row in self.client.get('/v1/model/info').json()['data']
                      if row['model_info']['id'] == model_id)
        self.assertEqual(stored['model_info']['subscription_assignment']['subscription_ids'], [first['subscription_id']])
        self.burst(name, 2, [first_identity])
        self.control(models_by_identity={})

    def test_stream_failure_or_cancellation_after_content_does_not_switch_members(self):
        first, _ = self.connect('StreamDistributionFirst')
        second, _ = self.connect('StreamDistributionSecond')
        for surface in ('responses', 'chat/completions'):
            for mode in ('failure', 'cancelled'):
                with self.subTest(surface=surface, mode=mode):
                    name, created = self.create_distribution([first, second])
                    self.assertEqual(created.status_code, 200, created.text)
                    self.control(stream_error=mode == 'failure', stream_error_at=6, delay=.1, clear=True)
                    body = {'model': name, 'stream': True, 'num_retries': 99}
                    body.update({'input': 'Interrupted distribution'} if surface == 'responses' else
                        {'messages': [{'role': 'user', 'content': 'Interrupted distribution'}]})
                    content_seen = False
                    with self.client.stream('POST', '/v1/' + surface, json=body) as response:
                        self.assertEqual(response.status_code, 200)
                        for line in response.iter_lines():
                            if line.startswith('data: ') and line != 'data: [DONE]':
                                event = json.loads(line[6:])
                                content = event.get('delta') if surface == 'responses' else next(
                                    (choice.get('delta', {}).get('content') for choice in event.get('choices', [])
                                     if choice.get('delta', {}).get('content')), None)
                                if content == 'ok':
                                    content_seen = True
                                    if mode == 'cancelled':
                                        break
                    self.assertTrue(content_seen, 'Synthetic text must reach the client before failure or cancellation')
                    row = self.wait_logs(name)['data'][0]
                    attempts = row['metadata']['subscription_attempts']
                    self.assertEqual(len(attempts), 1, row)
                    self.assertEqual(attempts[0]['outcome'], mode, row)
                    self.assertEqual(len(self.control()['requests']), 1)
                    self.control(stream_error=False, delay=0)

    @unittest.skipUnless(os.getenv('RESPONSE_CACHE_ENABLED') == 'true', 'requires isolated response cache')
    def test_cached_distribution_does_not_report_selected_candidates_as_attempts(self):
        first, _ = self.connect('CachedDistributionFirst')
        second, _ = self.connect('CachedDistributionSecond')
        name, created = self.create_distribution([first, second])
        self.assertEqual(created.status_code, 200, created.text)
        body = {'model': name, 'messages': [{'role': 'user', 'content': 'Cache one distributed result'}]}
        for count in range(1, 5):
            response = self.client.post('/v1/chat/completions', json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.wait_logs(name, count)
        rows = self.logs(name)['data']
        self.assertEqual(len(rows), 4)
        cached = [row for row in rows if row['cache_hit'] == 'True']
        self.assertGreaterEqual(len(cached), 2, rows)
        for row in cached:
            self.assertFalse(row['metadata'].get('subscription_attempts'), row)
            self.assertFalse(row['metadata'].get('subscription_ids'), row)
        physical = [row for row in rows if row['cache_hit'] != 'True']
        self.assertEqual(len(self.control()['requests']), len(physical))
        self.assertTrue(all(len(row['metadata']['subscription_attempts']) == 1 for row in physical))

    def test_distribution_assignment_survives_restart(self):
        if os.getenv('SUBSCRIPTION_RESTART_PHASE') != 'read':
            first, _ = self.connect('PersistentDistributionFirst')
            second, _ = self.connect('PersistentDistributionSecond')
            name, created = self.create_distribution([first, second])
            self.assertEqual(created.status_code, 200, created.text)
            model_id = created.json()['model_id']
            persistent_name = 'distribution-restart-' + first['subscription_id']
            updated = self.client.patch('/model/' + model_id + '/update', json={'model_name': persistent_name})
            self.assertEqual(updated.status_code, 200, updated.text)
            self.burst(persistent_name, 32, [first['account_id'], second['account_id']])
            self.wait_logs(persistent_name, 32)
            self.models.remove(model_id)
            return
        stored = [row for row in self.client.get('/v1/model/info').json()['data']
                  if row['model_name'].startswith('distribution-restart-')]
        self.assertEqual(len(stored), 1)
        parent = stored[0]
        name, model_id = parent['model_name'], parent['model_info']['id']
        self.models.append(model_id)
        members = parent['model_info']['subscription_assignment']['subscription_ids']
        self.assertEqual(parent['model_info']['subscription_assignment']['mode'], 'distribution')
        accounts = self.client.get('/chatgpt/api/accounts').json()['accounts']
        selected = [account for account in accounts if account['subscription_id'] in members]
        self.assertEqual(len(selected), 2)
        before = self.logs(name, page_size=100)['data']
        self.assertEqual(len(before), 32)
        old_attempts = [row['metadata']['subscription_attempts'][0] for row in before]
        old_ids = {attempt['attempt_id'] for attempt in old_attempts}
        old_members = {attempt['subscription_id']: attempt['model_id'] for attempt in old_attempts}
        self.burst(name, 32, [account['account_id'] for account in selected])
        for _ in range(80):
            rows = self.logs(name, page_size=100)['data']
            if len(rows) == 64:
                break
            time.sleep(.5)
        self.assertEqual(len(rows), 64)
        new_attempts = [row['metadata']['subscription_attempts'][0] for row in rows
                        if row['metadata']['subscription_attempts'][0]['attempt_id'] not in old_ids]
        self.assertEqual(len(new_attempts), 32)
        self.assertEqual({attempt['subscription_id']: attempt['model_id'] for attempt in new_attempts}, old_members)
        self.assertTrue(all(row['model_id'] == model_id for row in rows))

    def test_client_errors_exhausted_capacity_and_cooldown_recovery_are_bounded(self):
        import time
        first, first_identity = self.connect('CapacityFirst')
        second, second_identity = self.connect('CapacitySecond')
        for status in (400, 429):
            name, created = self.create_distribution([first, second])
            self.assertEqual(created.status_code, 200, created.text)
            self.control(status=status, statuses=[], clear=True)
            response = self.completion(name, num_retries=99,
                fallbacks=[{name: ['fixture-model']}], disable_fallbacks=False)
            self.assertGreaterEqual(response.status_code, 400, response.text)
            actual = self.control()['requests']
            self.assertEqual(len(actual), 1 if status == 400 else 2, actual)
            self.assertEqual(len({row['identity'] for row in actual}), len(actual))
            self.control(status=200)
        name, created = self.create_distribution([first, second])
        self.assertEqual(created.status_code, 200, created.text)
        self.client.patch('/model/' + created.json()['model_id'] + '/update',
            json={'model_info': {'cooldown_time': 1}}).raise_for_status()
        self.control(statuses=[429, 200], retry_after_by_identity={first_identity: 2, second_identity: 2}, clear=True)
        response = self.completion(name)
        self.assertEqual(response.status_code, 200, response.text)
        failed, successful = self.control()['requests']
        self.assertNotEqual(failed['identity'], successful['identity'])
        self.control(clear=True)
        immediate = self.completion(name)
        self.assertEqual(immediate.status_code, 200, immediate.text)
        self.assertEqual(self.control()['seen'], [successful['identity']])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            recovered = self.completion(name)
            self.assertEqual(recovered.status_code, 200, recovered.text)
            if failed['identity'] in self.control()['seen']:
                break
            time.sleep(.1)
        else:
            self.fail('Recovered member did not rejoin after its native cooldown')

    def test_retry_uses_another_member_and_records_only_actual_attempts(self):
        first, _ = self.connect('RetryFirst')
        second, _ = self.connect('RetrySecond')
        name, created = self.create_distribution([first, second])
        self.assertEqual(created.status_code, 200, created.text)
        self.control(statuses=[503, 200], clear=True)
        response = self.completion(name, num_retries=99,
            fallbacks=[{name: ['fixture-model']}], disable_fallbacks=False)
        self.assertEqual(response.status_code, 200, response.text)
        actual = self.control()['requests']
        self.assertEqual([row['status'] for row in actual], [503, 200])
        self.assertEqual(len({row['identity'] for row in actual}), 2)
        rows = self.wait_logs(name)['data']
        self.assertEqual(len(rows), 1)
        attempts = rows[0]['metadata']['subscription_attempts']
        self.assertEqual([attempt['outcome'] for attempt in attempts], ['failure', 'success'])
        self.assertEqual([attempt['reason'] for attempt in attempts], ['initial', 'retry'])
        self.assertEqual({attempt['subscription_id'] for attempt in attempts},
                         {first['subscription_id'], second['subscription_id']})

    def test_concurrent_requests_share_only_assigned_members_and_log_actual_attempts(self):
        first, first_identity = self.connect('DistributionFirst')
        second, second_identity = self.connect('DistributionSecond')
        outside, outside_identity = self.connect('Unassigned')
        name, created = self.create_distribution([first, second])
        self.assertEqual(created.status_code, 200, created.text)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.completion(name, api_key='chatgpt-account:' + outside['account']), range(32)))
        self.assertTrue(all(response.status_code == 200 for response in responses),
                        [(response.status_code, response.text[:200]) for response in responses if response.status_code != 200])
        seen = self.control()['seen']
        self.assertEqual(len(seen), 32)
        self.assertEqual(set(seen), {first_identity, second_identity})
        self.assertNotIn(outside_identity, seen)
        rows = self.wait_logs(name, 32)['data']
        self.assertEqual(len(rows), 32)
        attempts = [row['metadata']['subscription_attempts'] for row in rows]
        self.assertTrue(all(len(row) == 1 for row in attempts))
        self.assertEqual({row[0]['subscription_id'] for row in attempts},
                         {first['subscription_id'], second['subscription_id']})
        self.assertEqual(len({row[0]['attempt_id'] for row in attempts}), 32)
        self.assertEqual(len({row['metadata']['subscription_logical_id'] for row in rows}), 32)


for _base in (model_tests.SubscriptionModelTests, log_tests.SubscriptionLogTests):
    for _name in tuple(_base.__dict__):
        if _name.startswith('test_'):
            setattr(SubscriptionDistributionTests, _name, None)

del _base
