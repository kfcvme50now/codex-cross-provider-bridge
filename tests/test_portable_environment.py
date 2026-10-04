from __future__ import annotations
import json
import os
import sqlite3
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import nullcontext

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codex_bridge_environment import default_codex_home, default_cc_switch_db, default_config_path, official_bridge_url
from codex_config_guard import inspect_config_route
from codex_hook_manager import install_lifecycle_hooks
from codex_lifecycle_hook import run_configured_route_repair, run_user_prompt_submit_hook
from codex_route_projection import project_known_route
from codex_official_bridge_config import project_official_bridge

BRIDGE='http://127.0.0.1:18122/v1'
UPSTREAM='http://127.0.0.1:18121'


class PortableEnvironmentTests(unittest.TestCase):
    def test_home_and_database_environment_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.dict(os.environ, {'CODEX_HOME':str(root/'codex'), 'CC_SWITCH_HOME':str(root/'cc'), 'CC_SWITCH_DB':str(root/'chosen.db')}):
                self.assertEqual(default_codex_home(),root/'codex')
                self.assertEqual(default_config_path(),root/'codex/config.toml')
                self.assertEqual(default_cc_switch_db(),root/'chosen.db')

    def test_official_custom_port_takeover_is_owned_and_idempotent(self):
        before='model="fixture-model"\nmodel_provider="cc-switch-official"\n[model_providers.cc-switch-official]\nname="OpenAI"\nbase_url="'+UPSTREAM+'/v1"\n'
        after,_=project_official_bridge(before,official_bridge_url(BRIDGE),UPSTREAM)
        self.assertEqual(tomllib.loads(after)['model_providers']['custom']['base_url'],official_bridge_url(BRIDGE))
        self.assertFalse(project_official_bridge(after,official_bridge_url(BRIDGE),UPSTREAM)[1])
        with self.assertRaises(ValueError):
            project_official_bridge(before.replace(UPSTREAM,'https://unmanaged.invalid'),official_bridge_url(BRIDGE),UPSTREAM)

    def test_non_loopback_or_credential_bridge_is_rejected(self):
        for url in ['http://external.invalid:18122/v1','http://user:password@127.0.0.1:18122/v1','http://127.0.0.1:18122/v1?token=fixture']:
            with self.subTest(url=url),self.assertRaises(ValueError):official_bridge_url(url)

    def test_third_party_projection_and_hook_use_configured_endpoints(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);db=root/'router.db';cp=root/'config.toml'
            direct='model_provider="custom"\nmodel="fixture-model"\n[model_providers.custom]\nname="Fixture"\nbase_url="https://fixture.invalid/v1"\nwire_api="responses"\n'
            with sqlite3.connect(db) as connection:
                connection.execute('create table providers(category text,settings_config text,app_type text,is_current int)')
                connection.execute('insert into providers values(?,?,?,?)',('cn_official',json.dumps({'config':direct}),'codex',1))
            connection.close()
            takeover=direct.replace('https://fixture.invalid/v1',UPSTREAM+'/v1');cp.write_text(takeover,encoding='utf-8')
            after,kind=project_known_route(takeover,db,BRIDGE,UPSTREAM)
            self.assertEqual(kind,'cc-switch')
            self.assertEqual(tomllib.loads(after)['model_providers']['custom']['base_url'],BRIDGE)
            self.assertEqual(project_known_route(after,db,BRIDGE,UPSTREAM)[0],after)
            route=inspect_config_route(cp,BRIDGE,UPSTREAM);self.assertTrue(route['eligibleForAutomaticBridgeRepair'])
            with patch.dict(os.environ,{'CC_SWITCH_DB':str(db),'CODEX_BRIDGE_UPSTREAM_URL':UPSTREAM}):
                with patch('socket.create_connection',return_value=nullcontext()) as connect:
                    self.assertTrue(run_configured_route_repair(cp,BRIDGE,None)['ok'])
                    connect.assert_called_once_with(('127.0.0.1',18121),timeout=1)
            self.assertEqual(cp.read_text(encoding='utf-8'),after)

    def test_official_hook_keeps_selected_non_default_port(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder);cp=home/'config.toml';cp.write_text(project_official_bridge('model="fixture-model"\n',official_bridge_url(BRIDGE),UPSTREAM)[0],encoding='utf-8')
            output=run_user_prompt_submit_hook({'session_id':'fixture-thread'},{'officialBridgeEnabled':True,'portableHistoryViaBridge':True},home,cp,home/'status.json',False,bridge_url=BRIDGE)
            self.assertEqual(output['continue'],True)
            status=json.loads((home/'status.json').read_text(encoding='utf-8'))
            self.assertEqual(status['routeAfter'],official_bridge_url(BRIDGE))
            self.assertEqual(status['result'],'route-ok')

    def test_hook_wrapper_persists_paths_and_updates_after_relocation(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder);script=home/'lifecycle.py';script.touch()
            kwargs=dict(codex_home=home,policy_path=home/'policy.json',config_path=home/'config.toml',status_path=home/'status.json',lifecycle_script=script,python_executable=Path(sys.executable),apply=True,bridge_url=BRIDGE,cc_switch_db=home/'router.db',cc_switch_url=UPSTREAM)
            result=install_lifecycle_hooks(**kwargs)
            command=Path(result['wrapperPath']).read_text(encoding='utf-8')
            self.assertIn(str(home/'router.db'),command)
            self.assertIn(UPSTREAM,command)
            relocated=home/'moved.py';relocated.touch();kwargs['lifecycle_script']=relocated
            result=install_lifecycle_hooks(**kwargs)
            self.assertIn(str(relocated),Path(result['wrapperPath']).read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main()
