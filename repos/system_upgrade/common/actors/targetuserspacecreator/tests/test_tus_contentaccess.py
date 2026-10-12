from unittest import mock

import pytest
from test_tus_mock_utils import MockContext, MockInputData

from leapp.exceptions import StopActorExecutionError
from leapp.libraries.actor import tus_contentaccess
from leapp.libraries.common.testutils import logger_mocked


class MockCustomRepoFile:
    def __init__(self, path):
        self.file = path


@pytest.mark.parametrize(('version', 'exp_str'), (
    ('9', '9-stream\n'),
    ('10', '10-stream\n'),
))
@pytest.mark.parametrize('fpath', ('/etc/dnf/vars/stream', '/foo/bar/fname'))
def test_adjust_dnf_stream_variable_writes(version, exp_str, fpath):
    context = MockContext()
    tus_contentaccess._adjust_dnf_stream_variable(context, version, varfile=fpath)
    assert context.written_files == {fpath: exp_str}


@pytest.mark.parametrize('error', [OSError('disk error'), FileNotFoundError('missing')])
def test_adjust_dnf_stream_variable_error(error):
    context = MockContext(open_raises=error)
    with pytest.raises(StopActorExecutionError):
        tus_contentaccess._adjust_dnf_stream_variable(context, '9')
    assert not context.written_files


def test_install_custom_repofiles():
    context = MockContext()
    repofiles = [MockCustomRepoFile('/home/user/a.repo'), MockCustomRepoFile('/tmp/sub/b.repo')]
    tus_contentaccess._install_custom_repofiles(context, repofiles)
    assert context.copy_to_calls == [
        ('/home/user/a.repo', '/etc/yum.repos.d/a.repo'),
        ('/tmp/sub/b.repo', '/etc/yum.repos.d/b.repo'),
    ]


def test_install_custom_repofiles_empty():
    context = MockContext()
    tus_contentaccess._install_custom_repofiles(context, [])
    assert not context.copy_to_calls


def _setup_establish(monkeypatch, distro_id, swap_spy, set_container_spy, switch_cert_spy, target_major='9'):
    monkeypatch.setattr(tus_contentaccess, 'get_target_major_version', lambda: target_major)
    monkeypatch.setattr(tus_contentaccess, 'get_target_version', lambda: '9.6')
    monkeypatch.setattr(tus_contentaccess, 'get_target_distro_id', lambda: distro_id)
    monkeypatch.setattr(tus_contentaccess.tus_rhui, 'perform_client_swap', swap_spy)
    monkeypatch.setattr(tus_contentaccess.rhsm, 'set_container_mode', set_container_spy)
    monkeypatch.setattr(tus_contentaccess.rhsm, 'switch_certificate', switch_cert_spy)
    monkeypatch.setattr(tus_contentaccess.api, 'current_logger', logger_mocked())


def test_establish_centos_with_rhui(monkeypatch):
    context = MockContext()
    swap_spy = mock.Mock()
    set_container_spy = mock.Mock()
    switch_cert_spy = mock.Mock()
    _setup_establish(monkeypatch, 'centos', swap_spy, set_container_spy, switch_cert_spy)

    rhui_info = object()
    rhsm_info = object()
    repofiles = [MockCustomRepoFile('/tmp/custom.repo')]
    inputs = MockInputData(rhui_info=rhui_info, skip_rhsm=True, rhsm_info=rhsm_info,
                           custom_repofiles=repofiles)

    tus_contentaccess.establish(context, inputs)

    swap_spy.assert_called_once_with(context, rhui_info, '9.6', True)
    switch_cert_spy.assert_called_once_with(context, rhsm_info)
    set_container_spy.assert_called_once_with(context)
    assert context.written_files == {'/etc/dnf/vars/stream': '9-stream\n'}
    assert context.copy_to_calls == [('/tmp/custom.repo', '/etc/yum.repos.d/custom.repo')]


def test_establish_rhel_no_rhui(monkeypatch):
    context = MockContext()
    swap_spy = mock.Mock()
    set_container_spy = mock.Mock()
    switch_cert_spy = mock.Mock()
    _setup_establish(monkeypatch, 'rhel', swap_spy, set_container_spy, switch_cert_spy, target_major='8')

    inputs = MockInputData(rhui_info=None, skip_rhsm=False, rhsm_info=object(), custom_repofiles=[])

    tus_contentaccess.establish(context, inputs)

    swap_spy.assert_not_called()
    set_container_spy.assert_called_once_with(context)
    switch_cert_spy.assert_called_once_with(context, inputs.rhsm_info)
    assert not context.written_files
    assert not context.copy_to_calls


def test_establish_rhel_with_rhui_no_stream(monkeypatch):
    context = MockContext()
    swap_spy = mock.Mock()
    set_container_spy = mock.Mock()
    switch_cert_spy = mock.Mock()
    _setup_establish(monkeypatch, 'rhel', swap_spy, set_container_spy, switch_cert_spy)

    rhui_info = object()
    inputs = MockInputData(rhui_info=rhui_info, skip_rhsm=False, rhsm_info=object(), custom_repofiles=[])

    tus_contentaccess.establish(context, inputs)

    swap_spy.assert_called_once_with(context, rhui_info, '9.6', False)
    assert not context.written_files
