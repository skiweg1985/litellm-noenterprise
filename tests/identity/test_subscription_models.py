"""Subscription model assignment through native model APIs and a synthetic provider."""
import time
import json
import os
import uuid
import unittest
import httpx

BASE = 'http://proxy:4000'
PROVIDER = 'http://chatgpt:8091'
AUTH = {'Authorization': 'Bearer sk-identity-test-only'}

class SubscriptionModelTests(unittest.TestCase):
    def setUp(self):
        self.client = httpx.Client(base_url=BASE, headers=AUTH, trust_env=False, timeout=30)
        self.addCleanup(self.client.close)
        self.models = []
        self.addCleanup(self.cleanup)
        self.control(catalog_error=False, status=200, clear=True)

    def cleanup(self):
        self.control(catalog_error=False, status=200)
        for model in self.models:
            self.client.post('/model/delete', json={'id': model})

    def control(self, **values):
        return httpx.post(PROVIDER+'/control', json=values, trust_env=False).json()

    def connect(self, label, slugs=None):
        identity = label + '-' + uuid.uuid4().hex[:8]
        self.control(identity=identity)
        if slugs is not None:
            self.control(models_by_identity={identity: slugs})
        response = self.client.post('/chatgpt/api/accounts', json={'display_name': label})
        self.assertEqual(response.status_code, 200, response.text)
        account = response.json()
        self.client.post('/chatgpt/api/accounts/'+account['account']+'/login').raise_for_status()
        for _ in range(100):
            overview = self.client.get('/chatgpt/api/accounts').json()
            account = next(row for row in overview['accounts'] if row['subscription_id']==account['subscription_id'])
            if account['token_valid']:
                return account, identity
            time.sleep(.1)
        self.fail('Synthetic sign-in did not complete')

    def create(self, account, slug='gpt-5.4'):
        name = 'subscription-model-'+uuid.uuid4().hex[:8]
        response = self.client.post('/model/new', json={'model_name':name, 'litellm_params':{'model':'chatgpt/'+slug}, 'model_info':{'subscription_assignment':{'mode':'fixed','subscription_ids':[account['subscription_id']]}}})
        if response.status_code == 200:
            self.models.append(response.json()['model_id'])
        return name,response

    def test_create_request_and_edit_use_selected_subscription(self):
        first, first_identity = self.connect('Primary')
        second, second_identity = self.connect('Alternative')
        name, response = self.create(first)
        self.assertEqual(response.status_code, 200, response.text)
        model_id = response.json()['model_id']
        result = self.client.post('/v1/chat/completions', json={'model':name,'messages':[{'role':'user','content':'Synthetic hello'}]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.control()['seen'], [first_identity])
        updated = self.client.patch('/model/'+model_id+'/update', json={'model_info':{'subscription_assignment':{'mode':'fixed','subscription_ids':[second['subscription_id']]}}})
        self.assertEqual(updated.status_code, 200, updated.text)
        result = self.client.post('/v1/chat/completions', json={'model':name,'messages':[{'role':'user','content':'Another synthetic hello'}]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.control()['seen'], [first_identity,second_identity])

    def test_unknown_or_disconnected_assignment_is_rejected(self):
        _, response = self.create({'subscription_id':str(uuid.uuid4())})
        self.assertEqual(response.status_code, 400, response.text)
        account = self.client.post('/chatgpt/api/accounts', json={'display_name':'Disconnected'}).json()
        _, response = self.create(account)
        self.assertEqual(response.status_code, 409, response.text)

    def test_catalog_only_model_uses_selected_subscription_in_playground_chat(self):
        self.addCleanup(self.control, models_by_identity={})
        self.connect('UnassignedCatalog')
        slug = 'gpt-6.1-catalog-only-synthetic'
        account, identity = self.connect('SelectedCatalog', [slug])
        self.assertIsNotNone(account['available_models'], account.get('available_models_error'))
        name = 'catalog-only-' + uuid.uuid4().hex[:8]
        created = self.client.post('/model/new', json={
            'model_name': name,
            'litellm_params': {'model': 'chatgpt/' + slug, 'custom_llm_provider': 'chatgpt'},
            'model_info': {'mode': 'chat', 'subscription_assignment': {
                'mode': 'fixed', 'subscription_ids': [account['subscription_id']],
            }},
        })
        self.assertEqual(created.status_code, 200, created.text)
        self.models.append(created.json()['model_id'])
        self.control(clear=True)
        for streaming in (False, True):
            with self.subTest(stream=streaming):
                response = self.client.post('/v1/chat/completions', json={
                    'model': name, 'messages': [{'role': 'user', 'content': 'Synthetic playground request'}],
                    'stream': streaming,
                })
                self.assertEqual(response.status_code, 200, response.text)
                if streaming:
                    chunks = [json.loads(line[6:]) for line in response.text.splitlines()
                              if line.startswith('data: ') and line != 'data: [DONE]']
                    content = ''.join(choice['delta'].get('content') or ''
                                      for chunk in chunks for choice in chunk.get('choices', []))
                else:
                    content = response.json()['choices'][0]['message']['content']
                self.assertEqual(content, 'ok')
        observed = self.control()
        self.assertEqual(observed['seen'], [identity, identity])
        self.assertTrue(all(row['credential_matches'] for row in observed['requests']))

    def test_fixed_assignment_cannot_be_overridden_or_fall_back_to_another_model(self):
        account, identity = self.connect('Fixed')
        other, other_identity = self.connect('Unassigned')
        name, created = self.create(account)
        self.assertEqual(created.status_code, 200, created.text)
        result = self.client.post('/v1/chat/completions', json={'model':name, 'api_key':'chatgpt-account:'+other['account'], 'messages':[{'role':'user','content':'Keep fixed'}]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.control()['seen'], [identity])
        self.control(status=503,clear=True)
        result = self.client.post('/v1/chat/completions', json={'model':name,'messages':[{'role':'user','content':'Fail fixed'}], 'fallbacks':[{'%s'%name:['fixture-model']}], 'disable_fallbacks':False, 'num_retries':0})
        self.assertGreaterEqual(result.status_code, 400, result.text)
        self.assertTrue(all(seen==identity for seen in self.control()['seen']))
        self.client.post('/chatgpt/api/accounts/'+account['account']+'/logout').raise_for_status()
        self.control(status=200,clear=True)
        result = self.client.post('/v1/chat/completions', json={'model':name,'messages':[{'role':'user','content':'Disconnected'}], 'fallbacks':[{'%s'%name:['fixture-model']}], 'num_retries':0})
        self.assertGreaterEqual(result.status_code, 400)
        self.assertEqual(self.control()['seen'], [])

    def test_model_catalog_and_all_native_write_paths_validate_assignment(self):
        account,_ = self.connect('Catalog')
        _, invalid = self.create(account,'not-in-catalog')
        self.assertEqual(invalid.status_code,400,invalid.text)
        name, created = self.create(account)
        self.assertEqual(created.status_code,200,created.text)
        model_id=created.json()['model_id']
        for method,path in [('PATCH','/model/'+model_id+'/update'),('POST','/model/update')]:
            response=self.client.request(method,path,json={'model_info':{'id':model_id,'subscription_assignment':{'mode':'fixed','subscription_ids':[str(uuid.uuid4())]}}})
            self.assertEqual(response.status_code,400,response.text)
        self.control(catalog_error=True)
        _, unavailable = self.create(account)
        self.assertEqual(unavailable.status_code,503,unavailable.text)
        visible=self.client.get('/v1/model/info')
        self.assertEqual(visible.status_code,200,visible.text)
        stored=next(row for row in visible.json()['data'] if row['model_info']['id']==model_id)
        self.assertEqual(stored['model_info']['subscription_assignment']['subscription_ids'],[account['subscription_id']])
        self.assertTrue(stored['model_info']['subscription_assignment']['revision'])

    def test_fixed_name_cannot_gain_an_unassigned_peer(self):
        account,_ = self.connect('Unique')
        name,created = self.create(account)
        self.assertEqual(created.status_code,200,created.text)
        peer=self.client.post('/model/new',json={'model_name':name,'litellm_params':{'model':'openai/gpt-4o-mini','mock_response':'unassigned'}})
        if peer.status_code==200:
            self.models.append(peer.json()['model_id'])
        self.assertEqual(peer.status_code,409,peer.text)

    def test_provider_token_cannot_create_or_edit_subscription_models(self):
        account,_ = self.connect('Authorization')
        name,created = self.create(account)
        self.assertEqual(created.status_code,200,created.text)
        model_id=created.json()['model_id']
        token=httpx.post('http://issuer:8080/control/access-token',json={'oid':'models-provider-admin','roles':['access','admin']},trust_env=False).json()['access_token']
        body={'model_name':'denied-'+uuid.uuid4().hex[:8],'litellm_params':{'model':'chatgpt/gpt-5.4','api_key':'chatgpt-account:'+account['account']},'model_info':{'id':model_id,'subscription_assignment':{'mode':'fixed','subscription_ids':[account['subscription_id']]}}}
        for method,path in [('POST','/model/new'),('PATCH','/model/'+model_id+'/update'),('POST','/model/update')]:
            response=self.client.request(method,path,json=body,headers={'Authorization':'Bearer '+token})
            self.assertEqual(response.status_code,403,response.text)

    def test_assignment_survives_restart(self):
        if os.getenv('SUBSCRIPTION_RESTART_PHASE') != 'read':
            account,identity = self.connect('Persistence')
            name = 'subscription-restart-' + account['subscription_id']
            response=self.client.post('/model/new',json={'model_name':name,'litellm_params':{'model':'chatgpt/gpt-5.4','timeout':17},'model_info':{'subscription_assignment':{'mode':'fixed','subscription_ids':[account['subscription_id']]}}})
            self.assertEqual(response.status_code,200,response.text)
            return
        accounts=self.client.get('/chatgpt/api/accounts').json()['accounts']
        account=next(row for row in accounts if row['display_name']=='Persistence')
        name='subscription-restart-'+account['subscription_id']
        response=self.client.post('/v1/chat/completions',json={'model':name,'messages':[{'role':'user','content':'After restart'}]})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.control()['seen'],[account['account_id']])
        info=self.client.get('/v1/model/info').json()['data']
        model=next(row for row in info if row['model_name']==name)
        self.assertEqual(model['model_info']['subscription_assignment']['subscription_ids'],[account['subscription_id']])
        self.models.append(model['model_info']['id'])

    def test_explicit_provider_switch_clears_assignment_and_preserves_other_settings(self):
        account,_ = self.connect('Switch')
        name,created = self.create(account)
        self.assertEqual(created.status_code,200,created.text)
        model_id=created.json()['model_id']
        updated=self.client.patch('/model/'+model_id+'/update',json={'litellm_params':{'model':'openai/gpt-4o-mini','api_key':'fixture-unused','mock_response':'switched','timeout':17},'model_info':{'subscription_assignment':None}})
        self.assertEqual(updated.status_code,200,updated.text)
        response=self.client.post('/v1/chat/completions',json={'model':name,'messages':[{'role':'user','content':'Different provider'}]})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['choices'][0]['message']['content'],'switched')
        model=next(row for row in self.client.get('/v1/model/info').json()['data'] if row['model_info']['id']==model_id)
        self.assertNotIn('subscription_assignment',model['model_info'])
        self.assertEqual(model['litellm_params']['timeout'],17)
