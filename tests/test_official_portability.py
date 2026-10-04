import copy
import http.client
import json
from pathlib import Path
import sys
import tempfile
import sqlite3
import time
from contextlib import nullcontext
from types import SimpleNamespace
import threading
import tomllib
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_cross_provider_bridge import is_retryable_portability_error, request_client_kind
from codex_official_bridge_config import project_official_bridge, OFFICIAL_BRIDGE_URL
from codex_lifecycle_hook import run_precompact_hook, run_session_start_hook, run_user_prompt_submit_hook
from codex_route_projection import project_known_route, start_route_maintenance
from test_codex_cross_provider_bridge import start_bridge, sample_payload
from test_lifecycle_policy import write_fixture


class FixtureUpstream(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.receipts.append((self.path, body, dict(self.headers)))
        status = 404 if self.server.fail_once and len(self.server.receipts) == 1 else 200
        reply = (b'{"error":{"message":"Item with id rs_other not found. Items are not persisted when store is false."}}'
                 if status == 404 else b'{"ok":true}')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)
    def log_message(self, *args): pass


class OfficialPortabilityTests(unittest.TestCase):
    def request(self, payload, path='/responses', fail_once=False, browser=False, desktop=False):
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), FixtureUpstream)
        upstream.receipts = []
        upstream.fail_once = fail_once
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        bridge, bridge_thread, temp = start_bridge(1)  # CC Switch must never receive it.
        bridge.official_upstream = urlsplit(f'http://127.0.0.1:{upstream.server_port}/backend-api/codex')
        bridge.model_override = 'gpt-6-luna'
        try:
            connection = http.client.HTTPConnection('127.0.0.1', bridge.server_port, timeout=4)
            identity = ({'originator': 'Codex Desktop', 'User-Agent': 'Codex Desktop/0.160.0 (Windows 10.0.26200; x86_64) dumb'} if desktop else {})
            connection.request('POST', '/__codex_official__' + path, json.dumps(payload), {
                'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0' if browser else 'codex_cli_rs/0.160.0',
                'Authorization': 'Bearer local-fixture-only', 'ChatGPT-Account-Id': 'fixture-account', **identity})
            response = connection.getresponse()
            status = response.status
            response.read()
            connection.close()
            return status, list(upstream.receipts)
        finally:
            bridge.shutdown(); bridge.server_close(); bridge_thread.join(5); temp.cleanup()
            upstream.shutdown(); upstream.server_close(); thread.join(5)

    def test_normal_auto_and_manual_compaction_use_portable_current_model(self):
        for suffix, extra in [('/responses', {}), ('/responses', {'auto': True}), ('/responses/compact', {})]:
            with self.subTest(suffix=suffix, extra=extra):
                payload = {**sample_payload(), 'model': 'deepseek-flash'}
                if extra:
                    payload['input'].append({'type': 'compaction_trigger'})
                original = copy.deepcopy(payload)
                status, receipts = self.request(payload, suffix)
                self.assertEqual(status, 200)
                self.assertEqual(len(receipts), 1)
                path, sent, headers = receipts[0]
                self.assertEqual(path, '/backend-api/codex' + suffix)
                self.assertEqual(sent['model'], 'gpt-6-luna')
                self.assertNotIn('previous_response_id', sent)
                self.assertFalse(sent['store'])
                self.assertTrue(all('id' not in item for item in sent['input']))
                self.assertNotIn('reasoning', [item['type'] for item in sent['input']])
                calls = [item for item in sent['input'] if 'call_id' in item]
                self.assertEqual(calls[0]['call_id'], calls[1]['call_id'])
                if extra:
                    self.assertEqual(sent['input'][-1], {'type': 'compaction_trigger'})
                self.assertEqual(headers['Authorization'], 'Bearer local-fixture-only')
                self.assertEqual(headers['ChatGPT-Account-Id'], 'fixture-account')
                self.assertEqual(payload, original)

    def test_other_provider_missing_item_retries_once_with_no_reasoning(self):
        payload = {'model': 'gpt-6-luna', 'input': [{'type': 'reasoning', 'id': 'rs_other', 'encrypted_content': 'foreign'},
                   {'type': 'message', 'id': 'msg_other', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'keep'}]}]}
        status, receipts = self.request(payload, fail_once=True)
        self.assertEqual(status, 200)
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[0][1]['input'], payload['input'])
        self.assertEqual(receipts[1][1]['input'], [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'keep'}]}])

    def test_native_reasoning_is_preserved_when_not_rejected(self):
        payload = {'model': 'gpt-6-luna', 'input': [{'type': 'reasoning', 'id': 'rs_native', 'encrypted_content': 'native'}]}
        _, receipts = self.request(payload)
        self.assertEqual(receipts[0][1]['input'], payload['input'])

    def test_browser_cannot_enter_official_compatibility_route(self):
        status, receipts = self.request(sample_payload(), browser=True)
        self.assertEqual(status, 403)
        self.assertEqual(receipts, [])

    def test_observed_native_desktop_identity_reaches_official_route(self):
        status, receipts = self.request(sample_payload(), desktop=True)
        self.assertEqual(status, 200)
        self.assertEqual(len(receipts), 1)

    def test_desktop_identity_does_not_override_browser_signals(self):
        for headers in [
            {'originator':'Codex Desktop', 'User-Agent':'Mozilla/5.0'},
            {'originator':'Codex Desktop', 'Origin':'https://chatgpt.com'},
        ]:
            self.assertEqual(request_client_kind(headers), 'browser')

    def test_general_404_and_auth_errors_are_not_portability_retried(self):
        for status, body in [(404, b'Endpoint not found'), (401, b'Item with id x not found. store false'), (429, b'Rate limit')]:
            self.assertFalse(is_retryable_portability_error(status, body))

    def test_official_projection_preserves_root_and_is_idempotent(self):
        before = 'model="gpt-6-luna"\n[features]\nrespect_system_proxy=true\n'
        after, _ = project_official_bridge(before)
        parsed = tomllib.loads(after)
        self.assertEqual(parsed['model'], 'gpt-6-luna')
        self.assertEqual(parsed['features'], {'respect_system_proxy': True})
        for alias in ('custom', 'cc-switch-official'):
            self.assertEqual(parsed['model_providers'][alias]['base_url'], OFFICIAL_BRIDGE_URL)
            self.assertFalse(parsed['model_providers'][alias]['supports_websockets'])
        self.assertFalse(project_official_bridge(after)[1])

    def test_official_takeover_address_is_owned_but_external_address_is_not(self):
        config=project_official_bridge('model="gpt-6-luna"\n')[0]
        takeover=config.replace(OFFICIAL_BRIDGE_URL, 'http://127.0.0.1:15721/v1')
        self.assertEqual(project_official_bridge(takeover)[0], config)
        with self.assertRaises(ValueError):
            project_official_bridge(config.replace(OFFICIAL_BRIDGE_URL, 'https://unmanaged.invalid/v1'))

    def test_cn_official_provider_gets_same_route_maintenance(self):
        with tempfile.TemporaryDirectory() as folder:
            database=Path(folder)/'cc-switch.db'
            config='model="deepseek-flash"\nmodel_provider="custom"\n[model_providers.custom]\nname="deepseek"\nbase_url="https://api.deepseek.com"\nwire_api="responses"\n'
            with sqlite3.connect(database) as connection:
                connection.execute('create table providers(category text,settings_config text,app_type text,is_current int)')
                connection.execute('insert into providers values(?,?,?,?)',('cn_official',json.dumps({'config':config}),'codex',1))
            connection.close()
            projected,kind=project_known_route(config,database)
            self.assertEqual(kind,'cc-switch')
            self.assertEqual(tomllib.loads(projected)['model_providers']['custom']['base_url'],'http://127.0.0.1:15722/v1')
            self.assertEqual(project_known_route(projected,database)[0],projected)

    def test_hooks_ensure_transport_without_rewriting_or_stopping_history(self):
        with tempfile.TemporaryDirectory() as folder:
            home, config, tid = write_fixture(Path(folder))
            config.write_text(project_official_bridge('model="gpt-6-luna"\n')[0])
            original = (home / 'rollout.jsonl').read_bytes()
            policy = {'officialBridgeEnabled': True, 'portableHistoryViaBridge': True}
            event = {'session_id': tid, 'trigger': 'auto'}
            with patch('codex_lifecycle_hook.run_configured_bridge_ensure', return_value={'ok': True}):
                result = run_precompact_hook(event, policy, home, config, home / 'status.json', True)
                self.assertTrue(result['continue'])
                result = run_session_start_hook(event, policy, home, config, home / 'status.json', True)
                self.assertTrue(result['continue'])
            self.assertEqual((home / 'rollout.jsonl').read_bytes(), original)

    def test_known_third_party_switch_projects_all_history_aliases_without_changing_upstream(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'cc-switch.db'
            config = ('model_provider="custom"\nmodel="third-model"\n[model_providers.custom]\n'
                      'name="Fixture"\nbase_url="https://fixture.invalid/v1"\nwire_api="responses"\n')
            connection = sqlite3.connect(database)
            connection.execute('create table providers(category text,settings_config text,app_type text,is_current int)')
            connection.execute('insert into providers values(?,?,?,?)', ('third_party', json.dumps({'config': config}), 'codex', 1))
            connection.commit()
            try:
                result, kind = project_known_route(config, database)
                self.assertEqual(kind, 'cc-switch')
                parsed = tomllib.loads(result)
                for alias in ('custom', 'cc-switch-official'):
                    self.assertEqual(parsed['model_providers'][alias]['base_url'], 'http://127.0.0.1:15722/v1')
                self.assertEqual(parsed['model'], 'third-model')
                self.assertEqual(project_known_route(result, database)[0], result)
                stored = connection.execute('select settings_config from providers').fetchone()[0]
                self.assertEqual(json.loads(stored)['config'], config)
                unknown = config.replace('fixture.invalid', 'unrelated.invalid')
                self.assertEqual(project_known_route(unknown, database), (unknown, 'unmanaged'))
            finally:
                connection.close()

    def test_existing_bridge_automatically_projects_a_provider_switch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / 'config.toml'
            config.write_text('model="gpt-6-luna"\n')
            (root / 'lifecycle-policy.json').write_text(json.dumps({'portableHistoryViaBridge': True}))
            database = root / 'cc-switch.db'
            third = ('model_provider="custom"\nmodel="fixture-model"\n[model_providers.custom]\n'
                     'name="Fixture"\nbase_url="https://fixture.invalid/v1"\nwire_api="responses"\n')
            connection = sqlite3.connect(database)
            connection.execute('create table providers(category text,settings_config text,app_type text,is_current int)')
            connection.execute('insert into providers values(?,?,?,?)', ('third_party', json.dumps({'config': third}), 'codex', 1))
            connection.commit(); connection.close()
            server = SimpleNamespace(policy_file=root / 'policy.json', codex_home=root, cc_switch_db=database)
            with patch('codex_route_projection.socket.create_connection', return_value=nullcontext()):
                stopped = start_route_maintenance(server)
                try:
                    for source, expected in [('model="gpt-6-luna"\n', '/__codex_official__'), (third, '/v1')]:
                        config.write_text(source)
                        deadline = time.monotonic() + 8
                        while time.monotonic() < deadline:
                            parsed = tomllib.loads(config.read_text())
                            block = parsed.get('model_providers', {}).get(parsed.get('model_provider'), {})
                            if block.get('base_url') == 'http://127.0.0.1:15722' + expected:
                                break
                            time.sleep(0.1)
                        else:
                            self.fail('Bridge did not project the changed provider')
                        self.assertNotIn('openai', parsed['model_providers'])
                finally:
                    stopped.set()


if __name__ == '__main__': unittest.main()
