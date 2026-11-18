"""Keep the deployed proxy from ending a valid gateway wait before the application."""
import re
import unittest

from backend.gotc.gateway import async_text, keys, transport
from ops.deploy import nginx as installer


class GatewayProxyTimeoutTests(unittest.TestCase):
    def test_proxy_read_timeout_covers_gateway_waits(self):
        # No response reaches Nginx while admission, connection, and the initial
        # upstream wait run. Leave room to deliver the application's own error.
        initial_wait = keys.QUEUE_WAIT + transport.CONNECT_TIMEOUT + transport.UPSTREAM_TIMEOUT
        old_block = installer.BEGIN + '    location ^~ /gotc/v1/ { proxy_read_timeout 330s; proxy_send_timeout 330s; }\n' + installer.END
        for existing in ('', old_block):
            with self.subTest(upgrading=bool(existing)):
                source = 'server {\n    listen 443 ssl http2;\n    server_name event.invalid;\n' + existing + '}\n'
                candidate = installer.nginx_candidate(source, 'https://event.invalid')
                self.assertEqual(installer.nginx_candidate(candidate, 'https://event.invalid'), candidate)
                location = re.search(r'location\s+\^~\s+/gotc/v1/\s*\{([^{}]*)\}', candidate)
                self.assertIsNotNone(location, 'The model gateway must retain its dedicated proxy location')
                directive = re.search(r'\bproxy_read_timeout\s+(\d+)(ms|s|m|h)?\s*;', location.group(1))
                self.assertIsNotNone(directive, 'The gateway needs an explicit proxy read timeout')
                read_seconds = int(directive.group(1)) * {None: 1, 'ms': .001, 's': 1, 'm': 60, 'h': 3600}[directive.group(2)]
                self.assertGreater(read_seconds, initial_wait, 'Nginx would end the initial gateway wait first')
                self.assertGreater(read_seconds, async_text.STREAM_IDLE_TIMEOUT, 'Nginx would hide the terminal SSE error')
                self.assertRegex(location.group(1), r'\bproxy_send_timeout\s+330s\s*;', 'Request upload timeout must remain unchanged')
                self.assertRegex(location.group(1), r'proxy_set_header\s+Upgrade\s+\$http_upgrade\s*;', 'Realtime WebSocket sessions need the upgrade header')
                self.assertRegex(location.group(1), r'proxy_set_header\s+Connection\s+\$gotc_connection_upgrade\s*;', 'Realtime WebSocket sessions need the upgrade header')


if __name__ == '__main__':
    unittest.main()
