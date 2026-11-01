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
