"""Deployment proxy limits stay aligned with bounded application endpoints."""
import importlib.util
from pathlib import Path
import re
import unittest

from app import CLAIM_REVIEW_BODY_LIMIT, IMPORT_BODY_LIMIT

ROOT=Path(__file__).resolve().parent.parent
SPEC=importlib.util.spec_from_file_location('gotc_deploy_install',ROOT/'deploy'/'install.py')
install=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install)


class DeployConfigTests(unittest.TestCase):
    def test_import_preview_has_a_dedicated_two_mib_proxy_limit(self):
        config=install.LOCATIONS
        exact=re.search(r'location = /gotc/api/admin/data/import/preview \{(?P<body>.*?)\n    \}',config,re.S)
        self.assertIsNotNone(exact)
        self.assertIn('proxy_pass http://127.0.0.1:5274;',exact.group('body'))
        self.assertIn('client_max_body_size 2m;',exact.group('body'))
        general=re.search(r'location \^~ /gotc/ \{(?P<body>.*?)\n    \}',config,re.S)
        self.assertIsNotNone(general)
        self.assertIn('client_max_body_size 32k;',general.group('body'))
        self.assertLess(config.index('location = /gotc/api/admin/data/import/preview'),
                        config.index('location ^~ /gotc/'))

    def test_claim_endpoints_match_application_caps_without_widening_defaults(self):
        config=install.LOCATIONS
        required_proxy_controls=[
            'proxy_pass http://127.0.0.1:5274;',
            'proxy_http_version 1.1;',
            'proxy_set_header Host $host;',
            'proxy_set_header X-Real-IP $remote_addr;',
            'proxy_set_header X-Forwarded-For $remote_addr;',
            'proxy_set_header X-Forwarded-Proto $scheme;',
            'proxy_read_timeout 30s;',
            'proxy_send_timeout 30s;',
        ]
        for path,app_limit in [('roster',IMPORT_BODY_LIMIT),('review',CLAIM_REVIEW_BODY_LIMIT)]:
            with self.subTest(endpoint=path):
                location=f'location = /gotc/api/admin/claims/{path}'
                blocks=re.findall(re.escape(location)+r' \{(.*?)\n    \}',config,re.S)
                self.assertEqual(len(blocks),1,'claim endpoint needs one exact proxy location')
                size=re.search(r'client_max_body_size (\d+)([km]);',blocks[0])
                self.assertIsNotNone(size)
                self.assertEqual(int(size[1])*{'k':1024,'m':1024*1024}[size[2]],app_limit)
                for directive in required_proxy_controls:self.assertIn(directive,blocks[0])
                self.assertLess(config.index(location),config.index('location ^~ /gotc/'))
        general=re.search(r'location \^~ /gotc/ \{(.*?)\n    \}',config,re.S)
        self.assertIn('client_max_body_size 32k;',general[1])
        self.assertNotRegex(config,r'location (?:\^~ )?/gotc/api/admin/claims/')
        gateway=re.search(r'location \^~ /gotc/v1/ \{(.*?)\n    \}',config,re.S)
        self.assertIn('client_max_body_size 32m;',gateway[1])

    def test_existing_managed_block_is_upgraded_and_idempotent(self):
        original=('server {\n'
                  '    listen 443 ssl;\n'
                  '    server_name 8.141.114.201;\n'
                  '    location /other { proxy_pass http://127.0.0.1:9000; }\n'
                  +install.BEGIN+
                  '    location = /gotc { return 308 /gotc/; }\n'
                  '    location ^~ /gotc/ { client_max_body_size 32k; }\n'+
                  install.END+'}\n')
        upgraded=install.nginx_candidate(original)
        self.assertIn('location /other',upgraded)
        self.assertEqual(upgraded.count('location = /gotc/api/admin/data/import/preview'),1)
        self.assertEqual(upgraded.count('location = /gotc/api/admin/claims/roster'),1)
        self.assertEqual(upgraded.count('location = /gotc/api/admin/claims/review'),1)
        self.assertIn('client_max_body_size 2m;',upgraded)
        self.assertIn('client_max_body_size 32k;',upgraded)
        self.assertEqual(install.nginx_candidate(upgraded),upgraded)


if __name__=='__main__':
    unittest.main()
