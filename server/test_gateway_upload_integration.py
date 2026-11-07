import unittest
import urllib.error
import urllib.request
from unittest import mock

import keys
from gateway_uploads import LARGE_BODY_THRESHOLD, UploadDrainTimeout, UploadQueueFull


class IntegrationTests(unittest.TestCase):
    def test_queue_timeout_is_rate_limit_before_any_upload(self):
        for handler in (keys._HTTPHandler(), keys._HTTPSHandler()):
            with mock.patch.object(handler, 'do_open', side_effect=UploadQueueFull()):
                method = handler.https_open if isinstance(handler, keys._HTTPSHandler) else handler.http_open
                with self.assertRaises(keys.GatewayError) as caught:
                    method(urllib.request.Request('https://ai.gitee.com/v1/chat/completions'))
                self.assertEqual(caught.exception.status, 429)
                self.assertEqual(caught.exception.retry_after, 5)

    def test_wrapped_drain_timeout_means_request_may_be_billed(self):
        self.assertTrue(keys.request_was_sent(urllib.error.URLError(UploadDrainTimeout())))
        self.assertFalse(keys.request_was_sent(urllib.error.URLError(ConnectionRefusedError())))

    def test_large_context_provider_overload_is_not_replayed(self):
        service = object.__new__(keys.KeyService)
        service.opener = mock.Mock()
        service.opener.open.side_effect = urllib.error.HTTPError('https://ai.gitee.com/v1/chat/completions', 429, 'busy', {}, None)
        request = urllib.request.Request('https://ai.gitee.com/v1/chat/completions', data=b'x' * LARGE_BODY_THRESHOLD)
        with self.assertRaises(urllib.error.HTTPError):
            service.open_with_retry(request)
        self.assertEqual(service.opener.open.call_count, 1)

    def test_small_requests_keep_existing_safe_retry_behavior(self):
        service = object.__new__(keys.KeyService)
        service.opener = mock.Mock()
        response = object()
        service.opener.open.side_effect = [urllib.error.HTTPError('https://ai.gitee.com/v1/chat/completions', 429, 'busy', {}, None), response]
        with mock.patch.object(keys, 'RETRY_DELAYS', (0,)):
            result = service.open_with_retry(urllib.request.Request('https://ai.gitee.com/v1/chat/completions', data=b'{}'))
        self.assertIs(result, response)
        self.assertEqual(service.opener.open.call_count, 2)


if __name__ == '__main__':
    unittest.main()
