import itertools
import os
import re
import shutil

from leapp.exceptions import StopActorExecutionError
from leapp.libraries.actor import constants, repoaccess
from leapp.libraries.common import mounting, rhsm, utils
from leapp.libraries.common.config import get_env, get_source_distro_id, get_target_distro_id
from leapp.libraries.common.config.version import get_target_major_version, get_target_version
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


def prepare_target_userspace(context, userspace_dir, enabled_repos, packages):
    """
    Implement the creation of the target userspace.
    """
    _backup_to_persistent_package_cache(userspace_dir)

    run(['rm', '-rf', userspace_dir])
    _create_target_userspace_directories(userspace_dir)

    target_major_version = get_target_major_version()
    install_root_dir = '/el{}target'.format(target_major_version)
    with mounting.BindMount(source=userspace_dir, target=os.path.join(context.base_dir, install_root_dir.lstrip('/'))):
        _restore_persistent_package_cache(userspace_dir)
        if not is_nogpgcheck_set():
            _import_gpg_keys(context, install_root_dir, target_major_version)

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
        try:
            context.call(cmd, callback_raw=utils.logging_handler)
        except CalledProcessError as exc:
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


def _create_target_userspace_directories(target_userspace):
    api.current_logger().debug('Creating target userspace directories.')
    try:
        utils.makedirs(target_userspace)
        api.current_logger().debug('Done creating target userspace directories.')
    except OSError:
        api.current_logger().error(
            'Failed to create temporary target userspace directories %s', target_userspace, exc_info=True)
        # This is an attempt for giving the user a chance to resolve it on their own
        raise StopActorExecutionError(
            message='Failed to prepare environment for package download while creating directories.',
            details={
                'hint': 'Please ensure that {directory} is empty and modifiable.'.format(directory=target_userspace)
            }
        )


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


def _get_target_userspace():
    return constants.TARGET_USERSPACE.format(get_target_major_version())


def _create_target_userspace(context, indata, packages, files, target_repoids):
    """Create the target userspace."""
    # Local import to avoid a circular import at module load time:
    # targetrhui imports bootstrap (for _get_target_userspace and
    # _create_target_userspace_directories), so bootstrap must not import
    # targetrhui at the top level.
    from leapp.libraries.actor import targetrhui

    target_path = _get_target_userspace()
    prepare_target_userspace(context, target_path, target_repoids, list(packages))
    repoaccess._prep_repository_access(context, target_path)

    with mounting.NspawnActions(base_dir=target_path) as target_context:
        _copy_files(target_context, files)
    dnfplugin.install(_get_target_userspace())

    # If we used only repofiles from leapp-rhui-<provider> then remove these as they provide
    # duplicit definitions as the target clients already installed in the target container
    if indata.rhui_info:
        api.current_logger().debug(
            'Target container should have access to content. '
            'Removing repofiles from leapp-rhui-<provider> from the target..'
        )
        setup_info = indata.rhui_info.target_client_setup_info
        if not setup_info.bootstrap_target_client:
            targetrhui._remove_injected_repofiles_from_our_rhui_packages(context, setup_info)

    # and do not forget to set the rhsm into the container mode again
    with mounting.NspawnActions(_get_target_userspace()) as target_context:
        rhsm.set_container_mode(target_context)
