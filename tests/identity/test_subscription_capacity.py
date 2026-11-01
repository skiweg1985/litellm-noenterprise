"""Native limits remain attached to one public model across subscription members."""
import concurrent.futures
import os
import time
import unittest

import test_subscription_distribution as distribution_tests
import test_subscription_logs as log_tests
import test_subscription_models as model_tests


@unittest.skipUnless(os.getenv('SUBSCRIPTION_CAPACITY_CHECKS') == 'true', 'requires native pre-call checks profile')
class SubscriptionCapacityTests(distribution_tests.SubscriptionDistributionTests):
    def limited_model(self, accounts, **limits):
        import uuid
        name = 'subscription-capacity-' + uuid.uuid4().hex[:8]
        response = self.client.post('/model/new', json={
            'model_name': name,
            'litellm_params': {'model': 'chatgpt/gpt-5.4', **limits},
            'model_info': {'subscription_assignment': {'mode': 'distribution',
                'subscription_ids': [a['subscription_id'] for a in accounts]}},
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.models.append(response.json()['model_id'])
        return name

    def test_parent_rpm_is_not_multiplied_by_subscription_members(self):
        self.assert_parent_limit(rpm=2)

    def test_parent_tpm_is_not_multiplied_by_subscription_members(self):
        self.assert_parent_limit(tpm=8)

    def assert_parent_limit(self, **limits):
        first, _ = self.connect('RateFirst')
        second, _ = self.connect('RateSecond')
        # Avoid the native minute-window boundary while observing three calls.
        while int(time.time()) % 60 >= 55:
            time.sleep(.1)
        name = self.limited_model([first, second], **limits)
        for _ in range(2):
            response = self.completion(name)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['usage']['total_tokens'], 4)
        response = self.completion(name)
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(len(self.control()['requests']), 2)

    def test_parent_parallel_limit_is_shared_when_another_member_becomes_eligible(self):
        first, first_identity = self.connect('ParallelFirst')
        second, second_identity = self.connect('ParallelSecond')
        name = self.limited_model([first, second], max_parallel_requests=1)
        self.control(delay=.2, clear=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            initial = pool.submit(self.completion, name)
            for _ in range(100):
                observed = self.control()
                if observed['requests'] and observed['active'] == 1:
                    break
                time.sleep(.01)
            else:
                self.fail('First request did not reach the synthetic provider')
            selected_identity = observed['requests'][0]['identity']
            selected = first if selected_identity == first_identity else second
            self.client.post('/chatgpt/api/accounts/' + selected['account'] + '/logout').raise_for_status()
            next_request = pool.submit(self.completion, name)
            for result in (initial.result(timeout=15), next_request.result(timeout=15)):
                self.assertEqual(result.status_code, 200, result.text)
        observed = self.control()
        self.assertEqual(set(observed['seen']), {first_identity, second_identity})
        self.assertEqual(observed['peak_active'], 1, observed['requests'])
        self.assertEqual(observed['active'], 0)
        self.control(delay=0)


for _base in (distribution_tests.SubscriptionDistributionTests, log_tests.SubscriptionLogTests, model_tests.SubscriptionModelTests):
    for _name in tuple(_base.__dict__):
        if _name.startswith('test_'):
            setattr(SubscriptionCapacityTests, _name, None)
del _base
