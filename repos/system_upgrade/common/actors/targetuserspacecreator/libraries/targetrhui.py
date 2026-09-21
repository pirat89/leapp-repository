import os

from leapp.exceptions import StopActorExecutionError
from leapp.libraries.actor import bootstrap, repoaccess
from leapp.libraries.common import repofileutils, utils
from leapp.libraries.common.config.version import get_target_major_version
from leapp.libraries.stdlib import api, CalledProcessError


def get_copy_location_from_copy_in_task(context_basepath, copy_task):
    basename = os.path.basename(copy_task.src)
    dest_in_container = os.path.join(context_basepath, copy_task.dst)
    if os.path.isdir(dest_in_container):
        return os.path.join(copy_task.dst, basename)
    return copy_task.dst


def _get_rhui_available_repoids(context, rhui_info):
    """
    Get repoids provided by the RHUI target clients

    :rtype: set[str]
    """
    # If we are upgrading a RHUI system, check what repositories are provided by the (already installed) target clients
    setup_info = rhui_info.target_client_setup_info
    target_content_access_files = set()
    if setup_info.bootstrap_target_client:
        target_content_access_files = repoaccess._query_rpm_for_pkg_files(context, rhui_info.target_client_pkg_names)

    def is_repofile(path):
        return os.path.dirname(path) == '/etc/yum.repos.d' and os.path.basename(path).endswith('.repo')

    def extract_repoid_from_line(line):
        return line.split(':', 1)[1].strip()

    target_ver = api.current_actor().configuration.version.target
    setup_tasks = rhui_info.target_client_setup_info.preinstall_tasks.files_to_copy_into_overlay

    yum_repos_d = context.full_path('/etc/yum.repos.d')
    all_repofiles = {os.path.join(yum_repos_d, path) for path in os.listdir(yum_repos_d) if path.endswith('.repo')}
    api.current_logger().debug('(RHUI Setup) All available repofiles: {0}'.format(' '.join(all_repofiles)))

    target_access_repofiles = {
        context.full_path(path) for path in target_content_access_files if is_repofile(path)
    }

    # Exclude repofiles used to setup the target rhui access as on some platforms the repos provided by
    # the client are not sufficient to install the client into target userspace (GCP)
    rhui_setup_repofile_tasks = [task for task in setup_tasks if task.src.endswith('repo')]
    rhui_setup_repofiles = (
        get_copy_location_from_copy_in_task(context.base_dir, copy) for copy in rhui_setup_repofile_tasks
    )
    rhui_setup_repofiles = {context.full_path(repofile) for repofile in rhui_setup_repofiles}

    foreign_repofiles = all_repofiles - target_access_repofiles - rhui_setup_repofiles

    api.current_logger().debug(
        'The following repofiles are considered as unknown to'
        ' the target RHUI content setup and will be ignored: {0}'.format(' '.join(foreign_repofiles))
    )

    # Rename non-client repofiles so they will not be recognized when running dnf repolist
    for foreign_repofile in foreign_repofiles:
        os.rename(foreign_repofile, '{0}.back'.format(foreign_repofile))

    rhui_repoids = set()
    try:
        dnf_cmd = [
            'dnf', 'repolist',
            '--releasever', target_ver, '-v',
            '--enablerepo', '*',
            '--disablerepo', '*-source-*',
            '--disablerepo', '*-debug-*',
        ]
        repolist_result = context.call(dnf_cmd)['stdout']
        repoid_lines = [line for line in repolist_result.split('\n') if line.startswith('Repo-id')]
        rhui_repoids.update({extract_repoid_from_line(line) for line in repoid_lines})

    except CalledProcessError as err:
        details = {'err': err.stderr, 'details': str(err)}
        raise StopActorExecutionError(
            message='Failed to retrieve repoids provided by target RHUI clients.',
            details=details
        )

    finally:
        # Revert the renaming of non-client repofiles
        for foreign_repofile in foreign_repofiles:
            os.rename('{0}.back'.format(foreign_repofile), foreign_repofile)

    return rhui_repoids


def _remove_injected_repofiles_from_our_rhui_packages(target_userspace_ctx, rhui_setup_info):
    target_userspace_path = bootstrap._get_target_userspace()
    for copy in rhui_setup_info.preinstall_tasks.files_to_copy_into_overlay:
        dst_in_container = get_copy_location_from_copy_in_task(target_userspace_path, copy)
        dst_in_container = dst_in_container.strip('/')
        dst_in_host = os.path.join(target_userspace_path, dst_in_container)

        if os.path.isfile(dst_in_host) and dst_in_host.endswith('.repo'):
            # The repofile might have been replaced by a new one provided by the RHUI client if names collide
            # Performance: Do the query here and not earlier, because we would be running rpm needlessly
            try:
                path_with_root = '/' + dst_in_container
                target_userspace_ctx.call(['rpm', '-q', '--whatprovides', path_with_root])
                api.current_logger().debug('Repofile {0} kept as it is owned by some RPM.'.format(dst_in_host))
            except CalledProcessError:
                # rpm exists with 1 if the file is not owned by any RPM. We might be catching all kinds of other
                # problems here, but still better than always removing repofiles.
                api.current_logger().debug('Removing repofile - not owned by any RPM: {0}'.format(dst_in_host))
                os.remove(dst_in_host)


def _apply_rhui_access_preinstall_tasks(context, rhui_setup_info):
    if rhui_setup_info.preinstall_tasks:
        api.current_logger().debug('Applying RHUI preinstall tasks.')
        preinstall_tasks = rhui_setup_info.preinstall_tasks

        for file_to_remove in preinstall_tasks.files_to_remove:
            api.current_logger().debug('Removing {0} from the scratch container.'.format(file_to_remove))
            context.remove(file_to_remove)

        for copy_info in preinstall_tasks.files_to_copy_into_overlay:
            api.current_logger().debug(
                'Copying {0} in {1} into the scratch container.'.format(copy_info.src, copy_info.dst)
            )
            context.makedirs(os.path.dirname(copy_info.dst), exists_ok=True)
            context.copy_to(copy_info.src, copy_info.dst)


def _apply_rhui_access_postinstall_tasks(context, rhui_setup_info):
    if rhui_setup_info.postinstall_tasks:
        api.current_logger().debug('Applying RHUI postinstall tasks.')
        for copy_info in rhui_setup_info.postinstall_tasks.files_to_copy:
            context.makedirs(os.path.dirname(copy_info.dst), exists_ok=True)
            debug_msg = 'Copying {0} to {1} (inside the scratch container).'
            api.current_logger().debug(debug_msg.format(copy_info.src, copy_info.dst))
            context.call(['cp', copy_info.src, copy_info.dst])


def _get_copied_repoids_to_enable(setup_info):
    """
    Parse the .repo files copied into the overlay and collect the repoids they define.

    Used to restrict the client-swap dnf transaction to only the repositories provided
    by the copied setup files (on some platforms the client-provided repos are not
    sufficient to install the target client - e.g. GCP).

    :raises StopActorExecutionError: if a copied repofile cannot be parsed.
    :rtype: set[str]
    """
    copy_tasks = setup_info.preinstall_tasks.files_to_copy_into_overlay
    copied_repofiles = [copy.src for copy in copy_tasks if copy.src.endswith('.repo')]
    copied_repoids = set()
    for repofile in copied_repofiles:
        try:
            repofile_contents = repofileutils.parse_repofile(repofile)
        except repofileutils.InvalidRepoDefinition as e:
            raise StopActorExecutionError(
                message="Failed to parse repositories for RHUI: {}".format(str(e)),
                details={
                    'hint': 'Ensure the repository definition is correct or remove it '
                            'if the repository is not required for the upgrade.'
                })
        copied_repoids.update(entry.repoid for entry in repofile_contents.data)
    return copied_repoids


def _build_client_swap_dnf_command(indata, setup_info, target_major_version):
    """
    Assemble the ``dnf ... shell`` command and its stdin transaction script that swap
    the source RHUI clients for the target ones.

    :returns: a ``(cmd, dnf_transaction_steps)`` tuple - the command list and the list
        of ``dnf shell`` transaction lines to feed on stdin.
    """
    cmd = ['dnf', '-y']

    if setup_info.enable_only_repoids_in_copied_files and setup_info.preinstall_tasks:
        copied_repoids = _get_copied_repoids_to_enable(setup_info)
        cmd += ['--disablerepo', '*']
        for copied_repoid in copied_repoids:
            cmd.extend(('--enablerepo', copied_repoid))

    cmd += [
        '--setopt=module_platform_id=platform:el{}'.format(target_major_version),
        '--setopt=keepcache=1',
        '--releasever', api.current_actor().configuration.version.target,
        '--disableplugin', 'subscription-manager',
        'shell'
    ]

    src_client_remove_steps = ['remove {0}'.format(client) for client in indata.rhui_info.src_client_pkg_names]
    target_client_install_steps = ['install {0}'.format(client) for client in indata.rhui_info.target_client_pkg_names]
    dnf_transaction_steps = src_client_remove_steps + target_client_install_steps + ['transaction run']

    return cmd, dnf_transaction_steps


def _swap_clients_in_dnf_shell(context, cmd, dnf_transaction_steps, indata):
    """
    Run the client-swap ``dnf shell`` transaction in the scratch container.

    :raises StopActorExecutionError: if the transaction fails (e.g. no accessible
        repository providing the RHUI clients).
    """
    try:
        dnf_shell_instructions = '\n'.join(dnf_transaction_steps)
        api.current_logger().debug(
            'Supplying the following instructions to the `dnf shell`: {}'.format(dnf_shell_instructions)
        )
        context.call(cmd, callback_raw=utils.logging_handler, stdin=dnf_shell_instructions)
    except CalledProcessError as error:
        api.current_logger().debug(
            'Failed to swap RHUI clients. This is likely because there are no repositories '
            ' containing RHUI clients enabled, or we cannot access them.'
        )
        api.current_logger().debug(error)

        swapping_clients_info_msg = 'Failed to swap `{0}` (source client{1}) with {2} (target client{3}).'
        swapping_clients_info_msg = swapping_clients_info_msg.format(
            ' '.join(indata.rhui_info.src_client_pkg_names),
            '' if len(indata.rhui_info.src_client_pkg_names) == 1 else 's',
            ' '.join(indata.rhui_info.target_client_pkg_names),
            '' if len(indata.rhui_info.target_client_pkg_names) == 1 else 's',
        )

        details = {
            'details': swapping_clients_info_msg,
            'error': str(error)
        }
        raise StopActorExecutionError(
            'Failed to swap RHUI clients to establish content access',
            details=details
        )


def _query_client_owned_files_or_stop(context, indata):
    """
    Return the set of files owned by the (now installed) target RHUI clients.

    :raises StopActorExecutionError: if the query fails, which most likely means the
        target clients were not installed during the client-swap step.
    :rtype: set[str]
    """
    try:
        return repoaccess._query_rpm_for_pkg_files(context, indata.rhui_info.target_client_pkg_names)
    except CalledProcessError as err:  # We failed to rpm -qf PKG, the PKG is most likely not installed
        api.current_logger().critical('Failed to query files owned by target RHUI clients (clients=%s). This is caused'
                                      ' by failing to install the target clients during the client-swap step.'
                                      ' Full error: %s', indata.rhui_info.target_client_pkg_names, err)

        target_major = get_target_major_version()
        plural_suffix = 's' if len(indata.rhui_info.target_client_pkg_names) > 1 else ''
        client_rpms = ', '.join(indata.rhui_info.target_client_pkg_names)
        msg = ('Could not find the RHEL {target_major} RHUI client rpm{plural_suffix} ({client_rpms})'
               ' in the cloud provider\'s client repository.')
        raise StopActorExecutionError(msg.format(target_major=target_major, plural_suffix=plural_suffix,
                                                 client_rpms=client_rpms))


def _cleanup_injected_setup_files(context, setup_info, files_owned_by_clients):
    """
    Remove injected setup repofiles that are neither owned by the target clients nor
    required to support the client operation, so we do not end up with duplicit repoids.
    """
    for copy_task in setup_info.preinstall_tasks.files_to_copy_into_overlay:
        dest = get_copy_location_from_copy_in_task(context.base_dir, copy_task)
        can_be_cleaned_up = copy_task.src not in setup_info.files_supporting_client_operation
        if dest not in files_owned_by_clients and can_be_cleaned_up:
            context.remove(dest)


def setup_target_rhui_access_if_needed(context, indata):
    if not indata.rhui_info:
        return

    target_major_version = get_target_major_version()
    userspace_dir = bootstrap._get_target_userspace()
    bootstrap._create_target_userspace_directories(userspace_dir)

    setup_info = indata.rhui_info.target_client_setup_info
    _apply_rhui_access_preinstall_tasks(context, setup_info)

    if not setup_info.bootstrap_target_client:
        # Installation of the target RHUI client is not possible and we bundle all necessary
        # files into the leapp-rhui-<provider> packages.
        api.current_logger().debug('Bootstrapping target RHUI client is disabled, leapp will rely '
                                   'only on files budled in leapp-rhui-<provider> package.')
        return

    cmd, dnf_transaction_steps = _build_client_swap_dnf_command(indata, setup_info, target_major_version)
    _swap_clients_in_dnf_shell(context, cmd, dnf_transaction_steps, indata)

    _apply_rhui_access_postinstall_tasks(context, setup_info)

    # Do a cleanup so there are not duplicit repoids
    files_owned_by_clients = _query_client_owned_files_or_stop(context, indata)
    _cleanup_injected_setup_files(context, setup_info, files_owned_by_clients)
