"""Keep the deployed proxy from ending a valid gateway wait before the application."""
import ast
import importlib.util
from pathlib import Path
import re
import unittest


class GatewayProxyTimeoutTests(unittest.TestCase):
    def test_proxy_read_timeout_covers_gateway_waits(self):
        project = Path(__file__).resolve().parent.parent
        names = {'UPSTREAM_TIMEOUT', 'CONNECT_TIMEOUT', 'STREAM_IDLE_TIMEOUT', 'QUEUE_WAIT'}
        assignments = ast.parse((project / 'server' / 'keys.py').read_text(encoding='utf-8')).body
        waits = {target.id: ast.literal_eval(statement.value)
                 for statement in assignments if isinstance(statement, ast.Assign)
                 for target in statement.targets if isinstance(target, ast.Name) and target.id in names}
        self.assertEqual(set(waits), names)
        self.assertTrue(all(type(value) in (int, float) and value > 0 for value in waits.values()))

        spec = importlib.util.spec_from_file_location('gateway_timeout_installer', project / 'deploy' / 'install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        # No response reaches Nginx while admission, connection, and the initial
        # upstream wait run. Leave room to deliver the application's own error.
        initial_wait = waits['QUEUE_WAIT'] + waits['CONNECT_TIMEOUT'] + waits['UPSTREAM_TIMEOUT']
        old_block = installer.BEGIN + '    location ^~ /gotc/v1/ { proxy_read_timeout 330s; proxy_send_timeout 330s; }\n' + installer.END
        for existing in ('', old_block):
            with self.subTest(upgrading=bool(existing)):
                source = 'server {\n    listen 443;\n    server_name 8.141.114.201;\n' + existing + '}\n'
                candidate = installer.nginx_candidate(source)
                self.assertEqual(installer.nginx_candidate(candidate), candidate)
                location = re.search(r'location\s+\^~\s+/gotc/v1/\s*\{([^{}]*)\}', candidate)
                self.assertIsNotNone(location, 'The model gateway must retain its dedicated proxy location')
                directive = re.search(r'\bproxy_read_timeout\s+(\d+)(ms|s|m|h)?\s*;', location.group(1))
                self.assertIsNotNone(directive, 'The gateway needs an explicit proxy read timeout')
                read_seconds = int(directive.group(1)) * {None: 1, 'ms': .001, 's': 1, 'm': 60, 'h': 3600}[directive.group(2)]
                self.assertGreater(read_seconds, initial_wait, 'Nginx would end the initial gateway wait first')
                self.assertGreater(read_seconds, waits['STREAM_IDLE_TIMEOUT'], 'Nginx would hide the terminal SSE error')
                self.assertRegex(location.group(1), r'\bproxy_send_timeout\s+330s\s*;', 'Request upload timeout must remain unchanged')


if __name__ == '__main__':
    unittest.main()
