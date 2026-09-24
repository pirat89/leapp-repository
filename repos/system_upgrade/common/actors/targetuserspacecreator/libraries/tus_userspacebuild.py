"""
Build the target-userspace container.

Single responsibility: turn an empty userspace directory into a usable target
container - restore/backup the persistent package cache, import GPG keys, run the
bootstrap ``dnf install`` (diagnosing failures into actionable hints), wire in
certificate/repository access, copy the requested host files in, and install the
leapp dnf plugin.

This module imports only shared leapp libraries plus the two leaf actor modules
it composes (``layout`` and ``repoaccess``). RHUI post-build cleanup and
re-entering rhsm container mode are deliberately kept out of here - they live in
the orchestrator (``userspacegen.perform``) so this module carries no RHUI or
orchestration concerns.
"""

import itertools
import os
import re
import shutil

from leapp.exceptions import StopActorExecutionError
from leapp.libraries.actor import tus_layout, tus_repoaccess
from leapp.libraries.common import mounting, rhsm, utils
from leapp.libraries.common.config import (
    get_env,
    get_source_distro_id,
    get_target_distro_id
)
from leapp.libraries.common.config.version import (
    get_target_major_version,
    get_target_version
)
from leapp.libraries.common.dnflibs import dnfplugin
from leapp.libraries.common.gpg import get_path_to_gpg_certs, is_nogpgcheck_set
from leapp.libraries.stdlib import api, CalledProcessError, config, run
from leapp.models import PkgManagerInfo, RepositoriesFacts

PERSISTENT_PACKAGE_CACHE_DIR = '/var/lib/leapp/persistent_package_cache'
DEDICATED_LEAPP_PART_URL = 'https://access.redhat.com/solutions/7011704'


def _restore_persistent_package_cache(userspace_dir):
    if get_env('LEAPP_DEVEL_USE_PERSISTENT_PACKAGE_CACHE', None) == '1':
        if not os.path.exists(PERSISTENT_PACKAGE_CACHE_DIR):
            return
        dst_cache = os.path.join(userspace_dir, 'var', 'cache', 'dnf')
        if os.path.exists(dst_cache):
            run(['rm', '-rf', dst_cache])
        shutil.move(PERSISTENT_PACKAGE_CACHE_DIR, dst_cache)
    # We always want to remove the persistent cache here to unclutter the system
    run(['rm', '-rf', PERSISTENT_PACKAGE_CACHE_DIR])


def _backup_to_persistent_package_cache(userspace_dir):
    if get_env('LEAPP_DEVEL_USE_PERSISTENT_PACKAGE_CACHE', None) == '1':
        # Clean up any dead bodies, just in case
        run(['rm', '-rf', PERSISTENT_PACKAGE_CACHE_DIR])
        src_cache = os.path.join(userspace_dir, 'var', 'cache', 'dnf')
        if os.path.exists(src_cache):
            shutil.move(src_cache, PERSISTENT_PACKAGE_CACHE_DIR)


def _import_gpg_keys(context, install_root_dir, target_major_version):
    certs_path = get_path_to_gpg_certs()
    # Import the target distro target version GPG key to be able to verify the
    # installation of initial packages
    try:
        # Import also any other keys provided by the customer in the same directory
        for certname in os.listdir(certs_path):
            cmd = ['rpm', '--root', install_root_dir, '--import', os.path.join(certs_path, certname)]
            context.call(cmd, callback_raw=utils.logging_handler)
    except CalledProcessError as exc:
        raise StopActorExecutionError(
            message=(
                'Unable to import GPG certificates to install RHEL {} userspace packages.'
                .format(target_major_version)
            ),
            details={'details': str(exc), 'stderr': exc.stderr}
        )


def _handle_transaction_err_msg_size(err):
    NO_SPACE_STR = 'more space needed on the'

    # Disk Requirements:
    #   At least <size> more space needed on the <path> filesystem.
    #
    missing_space = [line.strip() for line in err.stderr.split('\n') if NO_SPACE_STR in line]
    size_str = re.match(r'At least (.*) more space needed', missing_space[0]).group(1)
    message = 'There is not enough space on the file system hosting /var/lib/leapp.'
    hint = (
        'Increase the free space on the filesystem hosting'
        ' /var/lib/leapp by {} at minimum. It is suggested to provide'
        ' reasonably more space to be able to perform all planned actions'
        ' (e.g. when 200MB is missing, add 1700MB or more).\n\n'
        'It is also a good practice to create dedicated partition'
        ' for /var/lib/leapp when more space is needed, which can be'
        ' dropped after the system upgrade is fully completed'
        ' For more info, see: {}'
        .format(size_str, DEDICATED_LEAPP_PART_URL)
    )
    # we do not want to confuse customers by the orig msg speaking about
    # missing space on '/'. Skip the Disk Requirements section.
    # The information is part of the hint.
    details = {'hint': hint}

    raise StopActorExecutionError(message=message, details=details)


def _assemble_dnf_install_command(target_major_version, install_root_dir, enabled_repos, packages):
    """
    Build the ``dnf install`` command line for the target userspace bootstrap.

    Pure list builder with no container I/O, so the exact command shape can be
    unit-tested in isolation.

    :param target_major_version: major version of the target system
    :param install_root_dir: value for dnf ``--installroot`` inside the container
    :param enabled_repos: repoids to enable for the bootstrap transaction
    :type enabled_repos: list
    :param packages: packages to install into the target userspace
    :type packages: list
    :return: the assembled ``dnf install`` argv
    :rtype: list
    """
    repos_opt = [['--enablerepo', repo] for repo in enabled_repos]
    repos_opt = list(itertools.chain(*repos_opt))
    cmd = ['dnf', 'install', '-y']
    if is_nogpgcheck_set():
        cmd.append('--nogpgcheck')
    cmd += [
        '--setopt=module_platform_id=platform:el{}'.format(target_major_version),
        '--setopt=keepcache=1',
        '--releasever', api.current_actor().configuration.version.target,
        '--installroot', install_root_dir,
        '--disablerepo', '*'
        ] + repos_opt + packages
    if config.is_verbose():
        cmd.append('-v')
    if rhsm.skip_rhsm():
        cmd += ['--disableplugin', 'subscription-manager']
    return cmd


def _diagnose_dnf_install_failure(exc):
    """
    Translate a failed ``dnf install`` into a StopActorExecutionError.

    Inspects the failure and the environment and attaches the most useful
    remediation hint available: not-enough-disk-space raises immediately with a
    dedicated message; a proxy configured in dnf.conf or for an enabled
    repository sets a proxy hint; a CentOS-to-RHEL upgrade appends a
    target-version reminder to any existing hint. This function never returns -
    it always raises.

    :param exc: the failure raised by the ``dnf install`` transaction
    :type exc: CalledProcessError
    :raises StopActorExecutionError: always
    """
    target_major_version = get_target_major_version()
    message = 'Unable to install target \'{}\' {} userspace packages.'.format(
        get_target_distro_id(), target_major_version
    )
    details = {'details': str(exc), 'stderr': exc.stderr}

    if 'more space needed on the' in exc.stderr:
        # The stderr contains this error summary:
        # Disk Requirements:
        #   At least <size> more space needed on the <path> filesystem.
        _handle_transaction_err_msg_size(exc)

    # If a proxy was set in dnf config, it should be the reason why dnf
    # failed since leapp does not support updates behind proxy yet.
    for manager_info in api.consume(PkgManagerInfo):
        if manager_info.configured_proxies:
            details['hint'] = (
                'DNF failed to install userspace packages, likely due to the proxy '
                'configuration detected in the YUM/DNF configuration file. '
                'Make sure the proxy is properly configured in /etc/dnf/dnf.conf. '
                'It\'s also possible the proxy settings in the DNF configuration file are '
                'incompatible with the target system. A compatible configuration can be '
                'placed in /etc/leapp/files/dnf.conf which, if present, will be used during '
                'the upgrade instead of /etc/dnf/dnf.conf. '
                'In such case the configuration will also be applied to the target system.'
            )

    # Similarly if a proxy was set specifically for one of the repositories.
    for repo_facts in api.consume(RepositoriesFacts):
        for repo_file in repo_facts.repositories:
            if any(repo_data.proxy and repo_data.enabled for repo_data in repo_file.data):
                details['hint'] = (
                    'DNF failed to install userspace packages, likely due to the proxy '
                    'configuration detected in a repository configuration file.'
                )

    if get_source_distro_id() == 'centos' and get_target_distro_id() == 'rhel':
        check_rhel_release_hint = (
            'When upgrading and converting from Centos Stream to Red Hat Enterprise Linux'
            ' (RHEL), the automatically determined latest target version of RHEL \'{}\' might'
            ' not yet have been released. If so, specify the latest released RHEL version'
            ' manually using the --target-version commandline option.'
        ).format(get_target_version())

        if details.get('hint'):
            # keep the proxy hint, we don't know which one is the problem
            details['hint'] = f"{details['hint']}\n\n{check_rhel_release_hint}"
        else:
            details['hint'] = check_rhel_release_hint

    raise StopActorExecutionError(message=message, details=details)


def _run_dnf_install(context, cmd):
    """
    Run the assembled ``dnf install`` command inside the container.

    On failure, delegate to :func:`_diagnose_dnf_install_failure`, which always
    raises :class:`StopActorExecutionError` with the most specific remediation
    hint that can be inferred from the failure and the environment.

    :param context: the scratch container to run the transaction in
    :type context: mounting.IsolatedActions class
    :param cmd: the ``dnf install`` argv from :func:`_assemble_dnf_install_command`
    :type cmd: list
    """
    try:
        context.call(cmd, callback_raw=utils.logging_handler)
    except CalledProcessError as exc:
        _diagnose_dnf_install_failure(exc)


def prepare_target_userspace(context, userspace_dir, enabled_repos, packages):
    """
    Implement the creation of the target userspace.
    """
    _backup_to_persistent_package_cache(userspace_dir)

    run(['rm', '-rf', userspace_dir])
    tus_layout.create_target_userspace_directories(userspace_dir)

    target_major_version = get_target_major_version()
    install_root_dir = '/el{}target'.format(target_major_version)
    with mounting.BindMount(source=userspace_dir, target=os.path.join(context.base_dir, install_root_dir.lstrip('/'))):
        _restore_persistent_package_cache(userspace_dir)
        if not is_nogpgcheck_set():
            _import_gpg_keys(context, install_root_dir, target_major_version)

        cmd = _assemble_dnf_install_command(target_major_version, install_root_dir, enabled_repos, packages)
        _run_dnf_install(context, cmd)


def _copy_files(context, files):
    """
    Copy the files/dirs from the host to the `context` userspace

    :param context: An instance of a mounting.IsolatedActions class
    :type context: mounting.IsolatedActions class
    :param files: list of files that should be copied from the host to the context
    :type files: list of CopyFile
    """
    for file_task in files:
        if not file_task.dst:
            file_task.dst = file_task.src
        if os.path.isdir(file_task.src):
            context.remove_tree(file_task.dst)
            context.copytree_to(file_task.src, file_task.dst)
        else:
            context.copy_to(file_task.src, file_task.dst)


def build_target_userspace(context, packages, files, repoids, userspace_path):
    """
    Build the target userspace container.

    Install the bootstrap packages into a freshly created userspace, wire in
    certificate and repository access, copy the requested host files in, and
    install the leapp dnf plugin.

    RHUI post-build cleanup and re-entering rhsm container mode are the caller's
    responsibility (see :func:`userspacegen.perform`), so this module stays free
    of RHUI and orchestration concerns.

    :param context: the scratch container the userspace is built from
    :type context: mounting.IsolatedActions class
    :param packages: packages to install into the target userspace
    :param files: CopyFile tasks to copy from the host into the userspace
    :param repoids: target repoids to enable for the bootstrap transaction
    :param userspace_path: filesystem path the target userspace is created at
    """
    prepare_target_userspace(context, userspace_path, repoids, list(packages))
    tus_repoaccess.prep_repository_access(context, userspace_path)

    with mounting.NspawnActions(base_dir=userspace_path) as target_context:
        _copy_files(target_context, files)
    dnfplugin.install(userspace_path)
