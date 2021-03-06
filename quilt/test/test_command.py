"""
Tests for commands.
"""

from datetime import datetime
import json
import os
import pytest
import requests
import responses

from six import assertRaisesRegex

try:
    import h5py
except ImportError:
    h5py = None

from quilt.tools import command, store
from .utils import QuiltTestCase, patch

class CommandTest(QuiltTestCase):
    def test_push_invalid_package(self):
        session = requests.Session()

        with assertRaisesRegex(self, command.CommandException, "owner/package_name"):
            command.push(session=session, package="no_user")
        with assertRaisesRegex(self, command.CommandException, "owner/package_name"):
            command.push(session=session, package="a/b/c")

    def test_install_invalid_package(self):
        session = requests.Session()

        with assertRaisesRegex(self, command.CommandException, "owner/package_name"):
            command.install(session=session, package="no_user")
        with assertRaisesRegex(self, command.CommandException, "owner/package_name"):
            command.install(session=session, package="a/b/c")

    def test_inspect_invalid_package(self):
        with assertRaisesRegex(self, command.CommandException, "owner/package_name"):
            command.inspect(package="no_user")
        with assertRaisesRegex(self, command.CommandException, "owner/package_name"):
            command.inspect(package="a/b/c")

    def test_push_missing_package(self):
        session = requests.Session()

        with assertRaisesRegex(self, command.CommandException, "not found"):
            command.push(session=session, package="owner/package")

    def test_inspect_missing_package(self):
        with assertRaisesRegex(self, command.CommandException, "not found"):
            command.inspect(package="owner/package")

    @patch('webbrowser.open')
    @patch('quilt.tools.command.input')
    @patch('quilt.tools.command._save_auth')
    def test_login(self, mock_save, mock_input, mock_open):
        old_refresh_token = "123"
        refresh_token = "456"
        access_token = "abc"
        expires_at = 1000.0

        mock_input.return_value = old_refresh_token

        self.requests_mock.add(
            responses.POST,
            '%s/api/token' % command.QUILT_PKG_URL,
            json=dict(
                status=200,
                refresh_token=refresh_token,
                access_token=access_token,
                expires_at=expires_at
            )
        )

        command.login()

        mock_open.assert_called_with('%s/login' % command.QUILT_PKG_URL)

        assert self.requests_mock.calls[0].request.body == "refresh_token=%s" % old_refresh_token

        mock_save.assert_called_with(dict(
            refresh_token=refresh_token,
            access_token=access_token,
            expires_at=expires_at
        ))

    @patch('webbrowser.open')
    @patch('quilt.tools.command.input')
    @patch('quilt.tools.command._save_auth')
    def test_login_server_error(self, mock_save, mock_input, mock_open):
        mock_input.return_value = "123"

        self.requests_mock.add(
            responses.POST,
            '%s/api/token' % command.QUILT_PKG_URL,
            status=500
        )

        with self.assertRaises(command.CommandException):
            command.login()

        mock_save.assert_not_called()

    @patch('webbrowser.open')
    @patch('quilt.tools.command.input')
    @patch('quilt.tools.command._save_auth')
    def test_login_auth_fail(self, mock_save, mock_input, mock_open):
        mock_input.return_value = "123"

        self.requests_mock.add(
            responses.POST,
            '%s/api/token' % command.QUILT_PKG_URL,
            json=dict(
                status=200,
                error="Bad token!"
            )
        )

        with self.assertRaises(command.CommandException):
            command.login()

        mock_save.assert_not_called()

    def test_ls(self):
        mydir = os.path.dirname(__file__)
        build_path = os.path.join(mydir, './build_simple.yml')
        command.build('foo/bar', build_path)

        command.ls()

    def test_inspect_valid_package(self):
        mydir = os.path.dirname(__file__)
        build_path = os.path.join(mydir, './build_simple.yml')
        command.build('foo/bar', build_path)

        command.inspect('foo/bar')

    def test_log(self):
        mydir = os.path.dirname(__file__)
        build_path = os.path.join(mydir, './build_simple.yml')
        owner = 'foo'
        package = 'bar'
        command.build('%s/%s' % (owner, package), build_path)

        pkg_obj = store.get_store(owner, package)
        self._mock_logs_list(owner, package, pkg_obj.get_hash())

        session = requests.Session()
        command.log(session, "{owner}/{pkg}".format(owner=owner, pkg=package))

    def _mock_logs_list(self, owner, package, pkg_hash):
        logs_url = "%s/api/log/%s/%s/" % (command.QUILT_PKG_URL, owner, package)
        resp = dict(logs=[dict(
            hash=pkg_hash,
            created=str(datetime.now()),
            author=owner)])
        print("MOCKING URL=%s" % logs_url)
        self.requests_mock.add(responses.GET, logs_url, json.dumps(resp))
