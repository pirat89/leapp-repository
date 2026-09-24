import itertools
import os
import re
import shutil

from leapp.exceptions import StopActorExecutionError
from leapp.libraries.actor import (
    tus_contentaccess,
    tus_inputdata,
    tus_layout,
    tus_repoaccess,
    tus_rhui,
    tus_targetrepos
)
from leapp.libraries.common import mounting, overlaygen, repofileutils, rhsm, utils
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
from leapp.models import TMPTargetRepositoriesFacts  # deprecated all the time
from leapp.models import (
    PkgManagerInfo,
    RepositoriesFacts,
    TargetOSInstallationImage,
    TargetUserSpaceInfo,
    UsedTargetRepositories,
    UsedTargetRepository
)
from leapp.utils.deprecation import suppress_deprecation

# TODO: "refactor" (modify) the library significantly
# The current shape is really bad and ineffective (duplicit parsing
# of repofiles). The library is doing 3 (5) things:
# # (0.) consume process input data
# # 1. prepare the first container, to be able to obtain repositories for the
# #    target system (this is extra neededwhen rhsm is used, but not reason to
# #    do such thing only when rhsm is used. Be persistent here
# # 2. gather target repositories that should AND can be used
# #    - basically here is the main thing that is PITA; I started
# #      the refactoring but realized that it needs much more changes because
# #      of RHSM...
# # 3. create the target userspace bootstrap
# # (4.) produce messages with the data
#
# Because of the lack of time, I am extending the current bad situation,
# but after the release, the related code should be really refactored.
# It would be probably ideal, if this and other actors in the current and the
# next phase are modified properly and we could create inhibitors in the check
# phase and keep everything on the report. But currently it seems it doesn't
# worth to invest so much energy into it. So let's just make this really
# readable (includes split of the functionality into several libraries)
# and do not mess.
# Issue: #486

PERSISTENT_PACKAGE_CACHE_DIR = '/var/lib/leapp/persistent_package_cache'
DEDICATED_LEAPP_PART_URL = 'https://access.redhat.com/solutions/7011704'


def _check_deprecated_rhsm_skip():
    # we do not plan to cover this case by tests as it is purely
    # devel/testing stuff, that becomes deprecated now
    # just log the warning now (better than nothing?); deprecation process will
    # be specified in close future
    if get_env('LEAPP_DEVEL_SKIP_RHSM', '0') == '1':
        api.current_logger().warning(
            'The LEAPP_DEVEL_SKIP_RHSM has been deprecated. Use'
            ' LEAPP_NO_RHSM instead or use the --no-rhsm option for'
            ' leapp. as well custom repofile has not been defined.'
            ' Please read documentation about new "skip rhsm" solution.'
        )


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
    tus_layout.create_target_userspace_directories(userspace_dir)

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


def _gather_target_repositories(context, indata):
    """
    Establish content access in the container, then gather the target repoids.

    The content-access setup (cert switch, container mode, CentOS $stream,
    custom repofiles) is delegated to :mod:`contentaccess`; this wrapper only
    sequences that command step before the discovery query.

    :param context: the container where the repofiles should be copied
    :type context: mounting.IsolatedActions class
    :param indata: majority of input data for the actor
    :type indata: inputdata.InputData
    """
    tus_contentaccess.prepare_repository_access(context, indata)
    return tus_targetrepos.select_target_repositories(context, indata)


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


def _create_target_userspace(context, indata, packages, files, target_repoids):
    """Create the target userspace."""
    target_path = tus_layout.target_userspace_path()
    prepare_target_userspace(context, target_path, target_repoids, list(packages))
    tus_repoaccess.prep_repository_access(context, target_path)

    with mounting.NspawnActions(base_dir=target_path) as target_context:
        _copy_files(target_context, files)
    dnfplugin.install(tus_layout.target_userspace_path())

    # If we used only repofiles from leapp-rhui-<provider> then remove these as they provide
    # duplicit definitions as the target clients already installed in the target container
    if indata.rhui_info:
        api.current_logger().debug(
            'Target container should have access to content. '
            'Removing repofiles from leapp-rhui-<provider> from the target..'
        )
        setup_info = indata.rhui_info.target_client_setup_info
        if not setup_info.bootstrap_target_client:
            tus_rhui.remove_injected_repofiles(context, setup_info)

    # and do not forget to set the rhsm into the container mode again
    with mounting.NspawnActions(tus_layout.target_userspace_path()) as target_context:
        rhsm.set_container_mode(target_context)


@suppress_deprecation(TMPTargetRepositoriesFacts)
def perform():
    # NOTE: this one action is out of unit-tests completely; we do not use
    # in unit tests the LEAPP_DEVEL_SKIP_RHSM envar anymore
    _check_deprecated_rhsm_skip()

    scratch_dir = os.getenv('LEAPP_CONTAINER_ROOT', '/var/lib/leapp/scratch')
    mounts_dir = os.path.join(scratch_dir, 'mounts')

    indata = tus_inputdata.InputData()
    reserve_space = overlaygen.get_recommended_leapp_free_space(tus_layout.target_userspace_path())
    with overlaygen.create_source_overlay(
            mounts_dir=mounts_dir,
            scratch_dir=scratch_dir,
            storage_info=indata.storage_info,
            xfs_info=indata.xfs_info,
            scratch_reserve=reserve_space) as overlay:
        with overlay.nspawn() as context:
            # Mount the ISO into the scratch container
            target_iso = next(api.consume(TargetOSInstallationImage), None)
            with mounting.mount_upgrade_iso_to_root_dir(overlay.target, target_iso):

                # TODO: this is out of tests completely
                tus_rhui.setup_target_rhui_access_if_needed(context, indata)

                target_repoids = _gather_target_repositories(context, indata)
                _create_target_userspace(context, indata, indata.packages, indata.files, target_repoids)
                # TODO: this is tmp solution as proper one needs significant refactoring
                try:
                    target_repo_facts = repofileutils.get_parsed_repofiles(context)
                except repofileutils.InvalidRepoDefinition as e:
                    raise StopActorExecutionError(
                        message="Failed to parse target system repofiles: {}".format(str(e)),
                        details={
                            'hint': 'Ensure the repository definition is correct or remove it '
                                    'if the repository is not needed anymore. '
                                    'This issue is typically caused by missing definition of the name field. '
                                    'For more information, see: https://access.redhat.com/solutions/6969001.'
                        })
                api.produce(TMPTargetRepositoriesFacts(repositories=target_repo_facts))
                # ## TODO ends here
                api.produce(UsedTargetRepositories(
                    repos=[UsedTargetRepository(repoid=repo) for repo in target_repoids]))
                api.produce(TargetUserSpaceInfo(
                    path=tus_layout.target_userspace_path(),
                    scratch=scratch_dir,
                    mounts=mounts_dir))
