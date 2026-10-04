import pathlib,sys,tempfile,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from codex_config_guard import inspect_config_route
class ConfigGuardTests(unittest.TestCase):
    def route(self,text):
        with tempfile.TemporaryDirectory() as temp:
            path=pathlib.Path(temp)/'config.toml';path.write_text(text,encoding='utf-8')
            return inspect_config_route(path)
    def test_gpt_on_third_party_loopback_requires_bridge(self):
        result=self.route('model="gpt-6-astra"\nmodel_provider="custom"\n[model_providers.custom]\nname="AnyRouter"\nbase_url="http://127.0.0.1:15721/v1"\n')
        self.assertTrue(result['eligibleForAutomaticBridgeRepair'])
        self.assertFalse(result['officialModelOrProvider'])
    def test_implicit_openai_is_native(self):
        self.assertFalse(self.route('model="gpt-6.1-sol"\n')['eligibleForAutomaticBridgeRepair'])
    def test_official_history_alias_is_native(self):
        result=self.route('model="gpt-6.1-sol"\nmodel_provider="custom"\n[model_providers.custom]\nname="OpenAI"\nwire_api="responses"\n')
        self.assertTrue(result['officialModelOrProvider'])
        self.assertFalse(result['eligibleForAutomaticBridgeRepair'])
    def test_remote_third_party_is_not_silently_taken_over(self):
        result=self.route('model="gpt-6-astra"\nmodel_provider="custom"\n[model_providers.custom]\nname="AnyRouter"\nbase_url="https://example.invalid/v1"\n')
        self.assertFalse(result['eligibleForAutomaticBridgeRepair'])
if __name__=='__main__':unittest.main()
