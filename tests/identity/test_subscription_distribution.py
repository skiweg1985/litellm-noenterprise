"""Distributed subscription models through native CRUD, inference and log APIs."""
import concurrent.futures
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
        self.control(status_by_identity={})

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
