import http.client,json,pathlib,sys,threading,unittest,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlsplit
import zstandard
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from codex_cross_provider_bridge import https_transport_connection,request_client_kind,load_policy,save_policy
from test_codex_cross_provider_bridge import start_bridge,sample_payload
class Recorder(BaseHTTPRequestHandler):
    def do_POST(self):
        body=self.rfile.read(int(self.headers.get('Content-Length',0)))
        self.server.receipts.append((body,dict(self.headers)))
        response=self.server.reply
        self.send_response(self.server.reply_status)
        self.send_header('Content-Type',self.server.reply_type)
        self.send_header('Content-Length',str(len(response)))
        self.end_headers();self.wfile.write(response)
    def log_message(self,*args):pass
class BridgeBoundaryTests(unittest.TestCase):
    def test_native_claude_sse_repair_is_disabled(self):
        body=b'{"model":"claude-example","messages":[],"stream":true}'
        reply=b'event: message_start\ndata: {"type":"message_start"}\n\n'
        receipts,policy,status=self.run_case({'Content-Type':'application/json','User-Agent':'claude-cli/2.1.280'},body,
            reply=reply,reply_type='text/event-stream',path='/v1/messages')
        self.assertEqual(receipts[0][0],body)
        self.assertTrue(policy.armed)
        self.assertFalse(status)

    def test_compaction_body_can_outlast_normal_stream_idle_limit(self):
        class DelayedCompact(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length',0)))
                reply=b'{"object":"response.compaction","output":[]}'
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(reply)))
                self.end_headers()
                self.wfile.flush()
                time.sleep(1.3)
                self.wfile.write(reply)
            def log_message(self,*args):pass
        upstream=ThreadingHTTPServer(('127.0.0.1',0),DelayedCompact)
        thread=threading.Thread(target=upstream.serve_forever,daemon=True);thread.start()
        bridge,bridge_thread,temp=start_bridge(upstream.server_port,upstream_idle_timeout=1)
        self.assertEqual(bridge.upstream_compact_idle_timeout,600)
        bridge.upstream_compact_idle_timeout=3
        try:
            client=http.client.HTTPConnection('127.0.0.1',bridge.server_port,timeout=5)
            client.request('POST','/v1/responses/compact',body=json.dumps(sample_payload()),
                headers={'Content-Type':'application/json','User-Agent':'codex_cli_rs/0.160.0'})
            response=client.getresponse()
            self.assertEqual(response.status,200)
            self.assertEqual(json.loads(response.read())['object'],'response.compaction')
            client.close()
        finally:
            bridge.shutdown();bridge.server_close();bridge_thread.join(5);temp.cleanup()
            upstream.shutdown();upstream.server_close();thread.join(5)

    def run_case(self,headers,body,expected_status=200,reply=b'unchanged response',reply_type='application/json',path='/v1/responses'):
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Recorder)
        upstream.receipts=[];upstream.reply=reply;upstream.reply_status=expected_status;upstream.reply_type=reply_type
        thread=threading.Thread(target=upstream.serve_forever,daemon=True);thread.start()
        bridge,bridge_thread,temp=start_bridge(upstream.server_port)
        try:
            policy=load_policy(bridge.policy_file);policy.scope='next';policy.armed=True;save_policy(bridge.policy_file,policy)
            bridge.model_override='SHOULD_ONLY_APPLY_TO_CODEX'
            client=http.client.HTTPConnection('127.0.0.1',bridge.server_port,timeout=5)
            client.request('POST',path,body=body,headers=headers)
            response=client.getresponse();result=response.read();client.close()
            self.assertEqual(response.status,expected_status);self.assertEqual(result,reply)
            receipts=list(upstream.receipts);policy=load_policy(bridge.policy_file)
            status_exists=bridge.status_file.exists()
            return receipts,policy,status_exists
        finally:
            bridge.shutdown();bridge.server_close();bridge_thread.join(5);temp.cleanup()
            upstream.shutdown();upstream.server_close();thread.join(5)
    def test_browser_is_byte_preserving_and_does_not_consume_next_or_retry(self):
        body=json.dumps(sample_payload(),indent=2).encode()
        receipts,policy,status=self.run_case({'Content-Type':'application/json','User-Agent':'Mozilla/5.0 Chrome/145.0','originator':'codex_app','Origin':'https://chatgpt.com'},body,400,b'{"error":{"param":"input[0].id","code":"invalid_value"}}')
        self.assertEqual([item[0] for item in receipts],[body])
        self.assertTrue(policy.armed);self.assertFalse(status)
    def test_unknown_client_and_model_prefix_are_not_repaired(self):
        payload=sample_payload();payload['model']='provider-id::gpt-6-astra';body=json.dumps(payload).encode()
        receipts,policy,status=self.run_case({'Content-Type':'application/json'},body)
        self.assertEqual(receipts[0][0],body);self.assertTrue(policy.armed);self.assertFalse(status)
    def test_browser_compressed_body_is_not_decoded_or_reserialized(self):
        body=zstandard.ZstdCompressor().compress(json.dumps(sample_payload()).encode())
        receipts,policy,status=self.run_case({'Content-Type':'application/json','Content-Encoding':'zstd','User-Agent':'Mozilla/5.0'},body)
        self.assertEqual(receipts[0][0],body);self.assertEqual(receipts[0][1]['Content-Encoding'],'zstd')
        self.assertTrue(policy.armed);self.assertFalse(status)
    def test_browser_claude_sse_is_not_reframed_or_given_synthetic_errors(self):
        body=b'{"model":"claude-fable-5-1-reversed","messages":[]}'
        reply=b'event: message_start\ndata: {"type":"message_start"}\n\n'
        receipts,policy,status=self.run_case({'Content-Type':'application/json','User-Agent':'Mozilla/5.0'},body,reply=reply,reply_type='text/event-stream',path='/v1/messages')
        self.assertEqual(receipts[0][0],body);self.assertTrue(policy.armed);self.assertFalse(status)
    def test_native_codex_still_repairs_and_consumes_next(self):
        receipts,policy,_=self.run_case({'Content-Type':'application/json','User-Agent':'codex_cli_rs/0.160.0'},json.dumps(sample_payload()).encode())
        sent=json.loads(receipts[0][0]);self.assertNotIn('previous_response_id',sent)
        self.assertEqual(sent['model'],'SHOULD_ONLY_APPLY_TO_CODEX');self.assertFalse(policy.armed)
class TransportProxyTests(unittest.TestCase):
    def test_compaction_v2_body_uses_compact_idle_limit(self):
        from codex_cross_provider_bridge import BridgeHandler
        from types import SimpleNamespace
        handler = object.__new__(BridgeHandler)
        handler.compaction_request = True
        handler.server = SimpleNamespace(upstream=urlsplit('http://127.0.0.1:12345'),
            upstream_header_timeout=600, upstream_idle_timeout=120, upstream_compact_idle_timeout=600)
        with patch('codex_cross_provider_bridge.http.client.HTTPConnection') as constructor:
            connection = constructor.return_value
            handler._forward('POST', '/backend-api/codex/responses', b'{}', {})
            connection.sock.settimeout.assert_called_once_with(600)
    def test_https_uses_configured_connect_proxy(self):
        with patch('codex_cross_provider_bridge.proxy_bypass',return_value=False),patch('codex_cross_provider_bridge.getproxies',return_value={'https':'http://127.0.0.1:10808'}),patch('codex_cross_provider_bridge.http.client.HTTPSConnection') as constructor:
            result=https_transport_connection(urlsplit('https://anyrouter.top/v1'),600)
            constructor.assert_called_once_with('127.0.0.1',10808,timeout=600)
            result.set_tunnel.assert_called_once_with('anyrouter.top',443,headers={})
    def test_loopback_ignores_global_proxy(self):
        with patch('codex_cross_provider_bridge.getproxies') as proxies,patch('codex_cross_provider_bridge.http.client.HTTPSConnection') as constructor:
            https_transport_connection(urlsplit('https://127.0.0.1:15721'),30)
            proxies.assert_not_called();constructor.assert_called_once_with('127.0.0.1',15721,timeout=30)
    def test_browser_identity_wins_over_codex_originator(self):
        self.assertEqual(request_client_kind({'User-Agent':'Mozilla/5.0','originator':'codex_cli_rs'}),'browser')
        self.assertEqual(request_client_kind({'Origin':'https://chatgpt.com','originator':'codex_app'}),'browser')
if __name__=='__main__':unittest.main()
