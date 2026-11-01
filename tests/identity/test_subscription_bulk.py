"""Review and apply subscription assignments through real admin HTTP endpoints."""

import unittest

import test_subscription_models as models


class SubscriptionBulkTests(models.SubscriptionModelTests):
    def setUp(self):
        super().setUp()
        self.control(models_by_identity={}, status_by_identity={})

    def preview(self, account):
        response = self.client.get('/chatgpt/api/accounts/' + account['account'] + '/model-assignments')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def apply(self, account, rows):
        return self.client.post('/chatgpt/api/accounts/' + account['account'] + '/model-assignments', json={'models': rows})

    def test_review_appends_selected_reserve_and_keeps_fixed_model_unchanged(self):
        first, first_identity = self.connect('Original')
        added, added_identity = self.connect('New')
        name, created = self.create(first)
        reserve_id = created.json()['model_id']
        self.client.patch('/model/' + reserve_id + '/update', json={'litellm_params': {'temperature': 0.3}, 'model_info': {'subscription_assignment': {'mode': 'priority', 'subscription_ids': [first['subscription_id']]}}}).raise_for_status()
        fixed_name, fixed = self.create(first)
        plan = self.preview(added)
        reserve = next(row for row in plan['models'] if row['model_id'] == reserve_id)
        fixed_row = next(row for row in plan['models'] if row['model_id'] == fixed.json()['model_id'])
        self.assertEqual(reserve['proposed']['subscription_ids'], [first['subscription_id'], added['subscription_id']])
        self.assertTrue(reserve['selected'])
        self.assertFalse(fixed_row['selected'])
        self.assertIsNone(fixed_row['proposed'])
        result = self.apply(added, [{'model_id': reserve_id, 'version': reserve['version'], 'action': 'append'}])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['results'][0]['status'], 'saved')
        stored = self.client.get('/v1/model/info').json()['data']
        row = next(row for row in stored if row['model_info']['id'] == reserve_id)
        self.assertEqual(row['litellm_params']['temperature'], 0.3)
        self.assertEqual(row['model_info']['subscription_assignment']['subscription_ids'], [first['subscription_id'], added['subscription_id']])
        self.control(status_by_identity={first_identity: 503}, clear=True)
        response = self.client.post('/v1/responses', json={'model': name, 'input': 'Use reviewed reserve'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.control()['seen'], [first_identity, added_identity])
        fixed_row = next(row for row in stored if row['model_name'] == fixed_name)
        self.assertEqual(fixed_row['model_info']['subscription_assignment']['subscription_ids'], [first['subscription_id']])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(SubscriptionBulkTests(name) for name in SubscriptionBulkTests.__dict__ if name.startswith('test_'))
