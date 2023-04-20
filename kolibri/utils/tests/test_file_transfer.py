import hashlib
import os
import tempfile
import unittest

from mock import call
from mock import MagicMock
from requests.exceptions import ChunkedEncodingError
from requests.exceptions import ConnectionError
from requests.exceptions import HTTPError
from requests.exceptions import RequestException
from requests.exceptions import Timeout

from kolibri.utils.file_transfer import FileCopy
from kolibri.utils.file_transfer import FileDownload
from kolibri.utils.file_transfer import retry_import
from kolibri.utils.file_transfer import RETRY_STATUS_CODE
from kolibri.utils.file_transfer import SSLERROR
from kolibri.utils.file_transfer import TransferFailed


class TestTransfer(unittest.TestCase):
    def setUp(self):
        self.source = "http://example.com/testfile"
        self.destdir = tempfile.mkdtemp()
        self.dest = self.destdir + "/test_file"
        self.content = b"test"
        hash = hashlib.md5()
        hash.update(self.content)
        self.checksum = hash.hexdigest()

    def tearDown(self):
        if os.path.exists(self.dest):
            os.remove(self.dest)

    def test_download_iterator(self):
        mock_response = MagicMock()
        mock_response.iter_content.return_value = iter([self.content])
        mock_response.headers = {"content-length": str(len(self.content))}
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response

        # Test FileDownload iterator
        with FileDownload(
            self.source, self.dest, self.checksum, session=mock_session
        ) as fd:
            for chunk in fd:
                self.assertEqual(chunk, self.content)

    def test_copy_iterator(self):
        self.copy_source = tempfile.NamedTemporaryFile(delete=False).name
        # Test FileCopy iterator
        with open(self.copy_source, "wb") as testfile:
            testfile.write(self.content)

        with FileCopy(self.copy_source, self.dest + "_copy", self.checksum) as fc:
            for chunk in fc:
                self.assertEqual(chunk, self.content)

        if os.path.exists(self.copy_source + "_copy"):
            os.remove(self.copy_source + "_copy")

    def test_download_checksum_validation(self):
        mock_response = MagicMock()
        mock_response.iter_content.return_value = iter([self.content])
        mock_response.headers = {"content-length": str(len(self.content))}
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response

        # Test FileDownload checksum validation
        with FileDownload(
            self.source, self.dest, self.checksum, session=mock_session
        ) as fd:
            for chunk in fd:
                pass
        self.assertTrue(os.path.isfile(self.dest))

    def test_copy_checksum_validation(self):
        self.copy_source = tempfile.NamedTemporaryFile(delete=False).name
        # Test FileCopy checksum validation
        with open(self.copy_source, "wb") as testfile:
            testfile.write(self.content)

        dest_copy = self.copy_source + "_copy"
        with FileCopy(self.copy_source, dest_copy, self.checksum) as fc:
            for chunk in fc:
                pass
        self.assertTrue(os.path.isfile(dest_copy))

        os.remove(dest_copy)

    def test_file_download_retry_resume(self):
        mock_response_1 = MagicMock()
        mock_response_1.raise_for_status.side_effect = ConnectionError
        mock_response_1.request.headers = {}
        mock_response_2 = MagicMock()
        mock_response_2.iter_content.return_value = iter([self.content])
        mock_response_2.headers = {"content-length": str(len(self.content))}
        mock_session = MagicMock()
        mock_session.get.side_effect = [
            mock_response_1,  # First call to requests.get
            mock_response_2,  # Second call to requests.get
        ]

        # Test retry and resume functionality in the FileDownload class
        with FileDownload(
            self.source, self.dest, self.checksum, session=mock_session, retry_wait=0
        ) as fd:
            for chunk in fd:
                pass

        self.assertTrue(os.path.isfile(self.dest))
        self.assertEqual(mock_session.get.call_count, 2)
        mock_session.get.assert_has_calls(
            [
                call(self.source, stream=True, timeout=60),
                call(
                    self.source, headers={"Range": "bytes=0-"}, stream=True, timeout=60
                ),
            ]
        )

        with open(self.dest, "rb") as f:
            downloaded_content = f.read()
        self.assertEqual(downloaded_content, self.content)

    def test_file_download_request_exception(self):
        mock_response = MagicMock()
        mock_response.iter_content.return_value = iter([self.content])
        mock_response.headers = {"content-length": str(len(self.content))}
        mock_session = MagicMock()
        mock_session.get.side_effect = RequestException

        # Test various exceptions during file downloads
        with self.assertRaises(RequestException):
            with FileDownload(
                self.source, self.dest, self.checksum, session=mock_session
            ) as fd:
                for chunk in fd:
                    pass
        self.assertFalse(os.path.isfile(self.dest))

    def test_file_download_checksum_exception(self):
        mock_response = MagicMock()
        mock_response.iter_content.return_value = iter([self.content])
        mock_response.headers = {"content-length": str(len(self.content))}
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response
        with self.assertRaises(TransferFailed):
            with FileDownload(
                self.source, self.dest, "invalid_checksum", session=mock_session
            ) as fd:
                for chunk in fd:
                    pass
        self.assertFalse(os.path.isfile(self.dest))


class TestRetryImport(unittest.TestCase):
    def _retry_import_helper(self, exception_class, *args, **kwargs):
        e = exception_class(*args, **kwargs)
        self.assertTrue(
            retry_import(e), "Expected retry for {}".format(exception_class.__name__)
        )

    def test_retry_import_connection_error(self):
        self._retry_import_helper(ConnectionError)

    def test_retry_import_timeout(self):
        self._retry_import_helper(Timeout)

    def test_retry_import_chunked_encoding_error(self):
        self._retry_import_helper(ChunkedEncodingError)

    def test_retry_import_http_error(self):
        for status_code in RETRY_STATUS_CODE:
            self._retry_import_helper(
                HTTPError, response=MagicMock(status_code=status_code)
            )

    def test_retry_import_ssl_error(self):
        self._retry_import_helper(SSLERROR, "decryption failed or bad record mac")

    def test_retry_import_non_retry_exceptions(self):
        non_retry_exceptions = [
            (RequestException, {}),
            (HTTPError, {"response": MagicMock(status_code=400)}),
        ]
        for exception_class, kwargs in non_retry_exceptions:
            e = exception_class(**kwargs)
            self.assertFalse(
                retry_import(e),
                "Expected no retry for {}".format(exception_class.__name__),
            )
