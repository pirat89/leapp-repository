"""
Characterization tests for the target-RHUI content-access setup path.

The RHUI client-swap path is cloud-only and historically had no unit coverage
(see CONTRACT.md section 13, "RHUI invariants"). These tests lock in the
*observable* behavior of the current implementation so the redesign that splits
the RHUI code out of ``userspacegen.py`` into its own module can be verified to
preserve it.

At this point in the redesign the functions under test still live in
``libraries/userspacegen.py``; the tests intentionally target their public entry
points (not the surrounding orchestration), so they keep working when the code
moves into a dedicated ``rhui.py`` module (the imports/patch targets are the only
thing that changes then).

The checklist items referenced below (R1-R6) come from CONTRACT.md section 13.
"""

import os

import pytest

from leapp.exceptions import StopActorExecutionError
from leapp.libraries.actor import tus_userspacegen
from leapp.libraries.common import repofileutils
from leapp.libraries.common.testutils import CurrentActorMocked, logger_mocked
from leapp.libraries.stdlib import api, CalledProcessError
from leapp.models import (
    CopyFile,
    RepositoryData,
    RepositoryFile,
    RHUIInfo,
    TargetRHUIPostInstallTasks,
    TargetRHUIPreInstallTasks,
    TargetRHUISetupInfo
)


def _called_process_error(cmd):
    return CalledProcessError(
        message='Command {0} failed'.format(cmd),
        command=cmd,
        result={'signal': None, 'exit_code': 1, 'pid': 0, 'stdout': 'out', 'stderr': 'err'},
    )


class RhuiContextMock:
    """
    Minimal stand-in for mounting.IsolatedActions covering only what the RHUI
    code touches: full_path, call (rpm -ql / dnf repolist / dnf shell / cp),
    remove, copy_to and makedirs. All side effects are appended to ``events`` in
    call order so ordering invariants can be asserted.
    """

    def __init__(self, base_dir='/target', repolist_stdout='', client_files=None,
                 fail_swap=False, fail_repolist=False, fail_client_query=False):
        self.base_dir = base_dir
        self._repolist_stdout = repolist_stdout
        self._client_files = [] if client_files is None else client_files
        self._fail_swap = fail_swap
        self._fail_repolist = fail_repolist
        self._fail_client_query = fail_client_query

        self.events = []
        self.removed = []
        self.copied_to = []
        self.makedirs_called = []
        self.swap_stdin = None

    def full_path(self, path):
        return os.path.join(self.base_dir, str(path).lstrip('/'))

    def call(self, cmd, **kwargs):
        self.events.append(('call', list(cmd)))
        if cmd[:2] == ['rpm', '-ql']:
            if self._fail_client_query:
                raise _called_process_error(cmd)
            return {'stdout': list(self._client_files)}
        if cmd[:2] == ['dnf', 'repolist']:
            if self._fail_repolist:
                raise _called_process_error(cmd)
            return {'stdout': self._repolist_stdout}
        if cmd[0] == 'dnf' and cmd[-1] == 'shell':
            self.swap_stdin = kwargs.get('stdin')
            if self._fail_swap:
                raise _called_process_error(cmd)
            return {'stdout': ''}
        if cmd[0] == 'cp':
            return {'stdout': ''}
        return {'stdout': ''}

    def remove(self, path):
        self.events.append(('remove', path))
        self.removed.append(path)

    def copy_to(self, src, dst):
        self.events.append(('copy_to', src, dst))
        self.copied_to.append((src, dst))

    def makedirs(self, path, exists_ok=False):
        self.events.append(('makedirs', path))
        self.makedirs_called.append(path)


def _copy(src, dst):
    return CopyFile(src=src, dst=dst)


def _setup_info(preinstall=None, postinstall=None, enable_only=True, bootstrap_client=True,
                supporting=None):
    return TargetRHUISetupInfo(
        enable_only_repoids_in_copied_files=enable_only,
        preinstall_tasks=preinstall or TargetRHUIPreInstallTasks(),
        postinstall_tasks=postinstall or TargetRHUIPostInstallTasks(),
        files_supporting_client_operation=supporting or [],
        bootstrap_target_client=bootstrap_client,
    )


def _rhui_info(setup_info, src_clients=None, target_clients=None):
    return RHUIInfo(
        provider='aws',
        src_client_pkg_names=src_clients or ['src-client'],
        target_client_pkg_names=target_clients or ['target-client'],
        target_client_setup_info=setup_info,
    )


def _indata(rhui_info):
    # setup_target_rhui_access_if_needed only reads indata.rhui_info.
    return type('InData', (), {'rhui_info': rhui_info})()


# ---------------------------------------------------------------------------
# R1 - get_copy_location_from_copy_in_task (pure path resolver)
# ---------------------------------------------------------------------------
def test_r1_copy_location_dst_is_dir(monkeypatch):
    # NOTE: dst is an absolute path, so os.path.join(basepath, dst) collapses to
    # dst itself - the container basepath is effectively ignored for the isdir
    # probe. This is the current behavior and the tests below rely on it.
    monkeypatch.setattr(os.path, 'isdir', lambda path: path == '/etc/yum.repos.d')
    task = _copy('/host/foo.repo', '/etc/yum.repos.d')
    # dst resolves to an existing directory => append the src basename
    assert tus_userspacegen.get_copy_location_from_copy_in_task('/base', task) == '/etc/yum.repos.d/foo.repo'


def test_r1_copy_location_dst_is_file(monkeypatch):
    monkeypatch.setattr(os.path, 'isdir', lambda path: False)
    task = _copy('/host/foo.repo', '/etc/yum.repos.d/bar.repo')
    # dst is not an existing directory => used verbatim
    assert tus_userspacegen.get_copy_location_from_copy_in_task('/base', task) == '/etc/yum.repos.d/bar.repo'


# ---------------------------------------------------------------------------
# R3 - _apply_rhui_access_preinstall_tasks (removals first, then copies)
# ---------------------------------------------------------------------------
def test_r3_preinstall_tasks_remove_then_copy(monkeypatch):
    monkeypatch.setattr(api, 'current_logger', logger_mocked())
    ctx = RhuiContextMock()
    preinstall = TargetRHUIPreInstallTasks(
        files_to_remove=['/etc/old.repo'],
        files_to_copy_into_overlay=[_copy('/host/a.repo', '/etc/yum.repos.d/a.repo')],
    )
    tus_userspacegen._apply_rhui_access_preinstall_tasks(ctx, _setup_info(preinstall=preinstall))

    assert ctx.removed == ['/etc/old.repo']
    assert ctx.copied_to == [('/host/a.repo', '/etc/yum.repos.d/a.repo')]
    assert ctx.makedirs_called == ['/etc/yum.repos.d']
    # removal must happen before the copy
    assert ctx.events.index(('remove', '/etc/old.repo')) < \
        ctx.events.index(('copy_to', '/host/a.repo', '/etc/yum.repos.d/a.repo'))


# ---------------------------------------------------------------------------
# R4 - _apply_rhui_access_postinstall_tasks (cp inside the container)
# ---------------------------------------------------------------------------
def test_r4_postinstall_tasks_copy_with_cp(monkeypatch):
    monkeypatch.setattr(api, 'current_logger', logger_mocked())
    ctx = RhuiContextMock()
    postinstall = TargetRHUIPostInstallTasks(
        files_to_copy=[_copy('/in/cert.pem', '/etc/pki/cert.pem')],
    )
    tus_userspacegen._apply_rhui_access_postinstall_tasks(ctx, _setup_info(postinstall=postinstall))

    assert ctx.makedirs_called == ['/etc/pki']
    assert ('call', ['cp', '/in/cert.pem', '/etc/pki/cert.pem']) in ctx.events


# ---------------------------------------------------------------------------
# R2 - _get_rhui_available_repoids (hide foreign repofiles around dnf repolist)
# ---------------------------------------------------------------------------
def _setup_r2(monkeypatch, ctx, repofiles_on_disk, client_repofiles):
    monkeypatch.setattr(api, 'current_logger', logger_mocked())
    monkeypatch.setattr(api, 'current_actor', CurrentActorMocked(dst_ver='9.4'))
    monkeypatch.setattr(os, 'listdir', lambda path: repofiles_on_disk)
    monkeypatch.setattr(os, 'rename', lambda src, dst: ctx.events.append(('rename', src, dst)))
    # target client owns these repofiles (returned by rpm -ql)
    ctx._client_files = client_repofiles


def test_r2_foreign_repofiles_hidden_and_restored(monkeypatch):
    ctx = RhuiContextMock(
        base_dir='/target',
        repolist_stdout='Repo-id  : rhui-target\nRepo-id : extra\nRepo-name : ignored\n',
    )
    yum_repos_d = ctx.full_path('/etc/yum.repos.d')
    _setup_r2(
        monkeypatch, ctx,
        repofiles_on_disk=['rhui-target.repo', 'foreign.repo'],
        client_repofiles=['/etc/yum.repos.d/rhui-target.repo', '/usr/share/doc/x'],
    )
    setup_info = _setup_info(preinstall=TargetRHUIPreInstallTasks())
    rhui_info = _rhui_info(setup_info)

    repoids = tus_userspacegen._get_rhui_available_repoids(ctx, rhui_info)

    assert repoids == {'rhui-target', 'extra'}

    foreign = os.path.join(yum_repos_d, 'foreign.repo')
    ev = ctx.events
    hide_idx = ev.index(('rename', foreign, foreign + '.back'))
    restore_idx = ev.index(('rename', foreign + '.back', foreign))
    repolist_idx = next(i for i, e in enumerate(ev) if e[0] == 'call' and e[1][:2] == ['dnf', 'repolist'])
    # foreign repofile hidden BEFORE repolist and restored AFTER
    assert hide_idx < repolist_idx < restore_idx
    # the client-owned repofile is never renamed
    assert not any(e[0] == 'rename' and 'rhui-target.repo' in e[1] for e in ctx.events)

    # dnf repolist flags are preserved
    assert ev[repolist_idx][1] == [
        'dnf', 'repolist', '--releasever', '9.4', '-v',
        '--enablerepo', '*', '--disablerepo', '*-source-*', '--disablerepo', '*-debug-*',
    ]


def test_r2_restores_foreign_repofiles_on_repolist_failure(monkeypatch):
    ctx = RhuiContextMock(base_dir='/target', fail_repolist=True)
    yum_repos_d = ctx.full_path('/etc/yum.repos.d')
    _setup_r2(
        monkeypatch, ctx,
        repofiles_on_disk=['foreign.repo'],
        client_repofiles=[],
    )
    rhui_info = _rhui_info(_setup_info(preinstall=TargetRHUIPreInstallTasks()))

    with pytest.raises(StopActorExecutionError) as err:
        tus_userspacegen._get_rhui_available_repoids(ctx, rhui_info)
    assert 'Failed to retrieve repoids provided by target RHUI clients' in str(err.value)

    # the finally-block must have restored the repofile despite the failure
    foreign = os.path.join(yum_repos_d, 'foreign.repo')
    assert ('rename', foreign, foreign + '.back') in ctx.events
    assert ('rename', foreign + '.back', foreign) in ctx.events


# ---------------------------------------------------------------------------
# R5 - setup_target_rhui_access_if_needed (top-level orchestrator)
# ---------------------------------------------------------------------------
def _patch_setup_env(monkeypatch):
    monkeypatch.setattr(api, 'current_logger', logger_mocked())
    monkeypatch.setattr(api, 'current_actor', CurrentActorMocked(dst_ver='9.4'))
    monkeypatch.setattr(tus_userspacegen, 'get_target_major_version', lambda: '9')
    monkeypatch.setattr(tus_userspacegen.tus_layout, 'target_userspace_path', lambda: '/target')
    monkeypatch.setattr(tus_userspacegen.tus_layout, 'create_target_userspace_directories', lambda path: None)


def test_r5_no_rhui_info_is_noop(monkeypatch):
    _patch_setup_env(monkeypatch)
    ctx = RhuiContextMock()
    tus_userspacegen.setup_target_rhui_access_if_needed(ctx, _indata(None))
    assert not ctx.events


def test_r5_bootstrap_disabled_only_applies_preinstall(monkeypatch):
    _patch_setup_env(monkeypatch)
    ctx = RhuiContextMock()
    preinstall = TargetRHUIPreInstallTasks(
        files_to_remove=['/etc/old.repo'],
        files_to_copy_into_overlay=[_copy('/host/a.repo', '/etc/yum.repos.d/a.repo')],
    )
    setup_info = _setup_info(preinstall=preinstall, bootstrap_client=False)
    tus_userspacegen.setup_target_rhui_access_if_needed(ctx, _indata(_rhui_info(setup_info)))

    # preinstall applied ...
    assert ctx.removed == ['/etc/old.repo']
    assert ctx.copied_to == [('/host/a.repo', '/etc/yum.repos.d/a.repo')]
    # ... but no client swap (no dnf shell call) is attempted
    assert not any(e[0] == 'call' and e[1][-1] == 'shell' for e in ctx.events)


def test_r5_client_swap_command_and_transaction(monkeypatch):
    _patch_setup_env(monkeypatch)
    monkeypatch.setattr(os.path, 'isdir', lambda path: False)
    ctx = RhuiContextMock(client_files=['/etc/yum.repos.d/a.repo'])
    preinstall = TargetRHUIPreInstallTasks(
        files_to_copy_into_overlay=[_copy('/host/a.repo', '/etc/yum.repos.d/a.repo')],
    )
    # enable_only disabled so no repofile parsing is needed for this assertion
    setup_info = _setup_info(preinstall=preinstall, enable_only=False,
                             supporting=['/host/a.repo'])
    rhui_info = _rhui_info(setup_info, src_clients=['src-a', 'src-b'], target_clients=['tgt'])

    tus_userspacegen.setup_target_rhui_access_if_needed(ctx, _indata(rhui_info))

    swap = [e[1] for e in ctx.events if e[0] == 'call' and e[1][-1] == 'shell'][0]
    # required dnf flags on the swap command
    assert swap[:2] == ['dnf', '-y']
    assert '--setopt=module_platform_id=platform:el9' in swap
    assert '--setopt=keepcache=1' in swap
    assert swap[swap.index('--releasever') + 1] == '9.4'
    assert swap[swap.index('--disableplugin') + 1] == 'subscription-manager'
    # enable_only disabled => no repo restriction injected
    assert '--disablerepo' not in swap

    # transaction order: remove sources -> install target -> run
    assert ctx.swap_stdin == 'remove src-a\nremove src-b\ninstall tgt\ntransaction run'


def test_r5_enable_only_repoids_restricts_to_copied_repofiles(monkeypatch):
    _patch_setup_env(monkeypatch)
    monkeypatch.setattr(os.path, 'isdir', lambda path: False)

    def fake_parse(repofile):
        assert repofile == '/host/a.repo'
        return RepositoryFile(file=repofile, data=[RepositoryData(repoid='copied-repo', name='n')])

    monkeypatch.setattr(repofileutils, 'parse_repofile', fake_parse)
    ctx = RhuiContextMock(client_files=['/etc/yum.repos.d/a.repo'])
    preinstall = TargetRHUIPreInstallTasks(
        files_to_copy_into_overlay=[_copy('/host/a.repo', '/etc/yum.repos.d/a.repo')],
    )
    setup_info = _setup_info(preinstall=preinstall, enable_only=True, supporting=['/host/a.repo'])
    tus_userspacegen.setup_target_rhui_access_if_needed(ctx, _indata(_rhui_info(setup_info)))

    swap = [e[1] for e in ctx.events if e[0] == 'call' and e[1][-1] == 'shell'][0]
    assert swap[swap.index('--disablerepo') + 1] == '*'
    assert swap[swap.index('--enablerepo') + 1] == 'copied-repo'


def test_r5_enable_only_invalid_repofile_stops(monkeypatch):
    _patch_setup_env(monkeypatch)

    def raise_invalid(repofile):
        raise repofileutils.InvalidRepoDefinition('bad', repofile=repofile, repoid='x')

    monkeypatch.setattr(repofileutils, 'parse_repofile', raise_invalid)
    ctx = RhuiContextMock()
    preinstall = TargetRHUIPreInstallTasks(
        files_to_copy_into_overlay=[_copy('/host/a.repo', '/etc/yum.repos.d/a.repo')],
    )
    setup_info = _setup_info(preinstall=preinstall, enable_only=True)
    with pytest.raises(StopActorExecutionError) as err:
        tus_userspacegen.setup_target_rhui_access_if_needed(ctx, _indata(_rhui_info(setup_info)))
    assert 'Failed to parse repositories for RHUI' in str(err.value)


def test_r5_swap_failure_stops(monkeypatch):
    _patch_setup_env(monkeypatch)
    ctx = RhuiContextMock(fail_swap=True)
    setup_info = _setup_info(preinstall=TargetRHUIPreInstallTasks(), enable_only=False)
    with pytest.raises(StopActorExecutionError) as err:
        tus_userspacegen.setup_target_rhui_access_if_needed(ctx, _indata(_rhui_info(setup_info)))
    assert 'Failed to swap RHUI clients to establish content access' in str(err.value)


def test_r5_client_query_failure_stops(monkeypatch):
    _patch_setup_env(monkeypatch)
    ctx = RhuiContextMock(fail_client_query=True)
    setup_info = _setup_info(preinstall=TargetRHUIPreInstallTasks(), enable_only=False)
    with pytest.raises(StopActorExecutionError) as err:
        tus_userspacegen.setup_target_rhui_access_if_needed(ctx, _indata(_rhui_info(setup_info)))
    assert 'Could not find the RHEL 9 RHUI client rpm' in str(err.value)


def test_r5_cleanup_removes_injected_unowned_setup_files(monkeypatch):
    _patch_setup_env(monkeypatch)
    monkeypatch.setattr(os.path, 'isdir', lambda path: False)
    # target client owns b.repo (dest inside container), but not c.repo
    ctx = RhuiContextMock(client_files=['/etc/yum.repos.d/b.repo'])
    preinstall = TargetRHUIPreInstallTasks(
        files_to_copy_into_overlay=[
            _copy('/host/a.repo', '/etc/yum.repos.d/a.repo'),   # supporting => kept
            _copy('/host/b.repo', '/etc/yum.repos.d/b.repo'),   # owned by client => kept
            _copy('/host/c.repo', '/etc/yum.repos.d/c.repo'),   # unowned + not supporting => removed
        ],
    )
    setup_info = _setup_info(preinstall=preinstall, enable_only=False,
                             supporting=['/host/a.repo'])
    tus_userspacegen.setup_target_rhui_access_if_needed(ctx, _indata(_rhui_info(setup_info)))

    # only the injected, unowned, non-supporting repofile is cleaned up
    assert '/etc/yum.repos.d/c.repo' in ctx.removed
    assert '/etc/yum.repos.d/a.repo' not in ctx.removed
    assert '/etc/yum.repos.d/b.repo' not in ctx.removed


# ---------------------------------------------------------------------------
# R6 - _remove_injected_repofiles_from_our_rhui_packages
# ---------------------------------------------------------------------------
def test_r6_removes_unowned_repofiles_keeps_owned(monkeypatch):
    monkeypatch.setattr(api, 'current_logger', logger_mocked())
    monkeypatch.setattr(tus_userspacegen.tus_layout, 'target_userspace_path', lambda: '/target')
    monkeypatch.setattr(os.path, 'isdir', lambda path: False)
    monkeypatch.setattr(os.path, 'isfile', lambda path: path.endswith('.repo'))

    removed = []
    monkeypatch.setattr(os, 'remove', removed.append)

    class Ctx:
        def call(self, cmd):
            # rpm -q --whatprovides /<owned> succeeds, others raise
            if cmd[:3] == ['rpm', '-q', '--whatprovides'] and cmd[3] == '/etc/yum.repos.d/owned.repo':
                return {'stdout': 'some-package'}
            raise _called_process_error(cmd)

    preinstall = TargetRHUIPreInstallTasks(
        files_to_copy_into_overlay=[
            _copy('/host/owned.repo', '/etc/yum.repos.d/owned.repo'),
            _copy('/host/unowned.repo', '/etc/yum.repos.d/unowned.repo'),
        ],
    )
    tus_userspacegen._remove_injected_repofiles_from_our_rhui_packages(Ctx(), _setup_info(preinstall=preinstall))

    assert removed == ['/target/etc/yum.repos.d/unowned.repo']
