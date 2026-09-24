import os

from leapp.exceptions import StopActorExecutionError
from leapp.libraries.actor import (
    tus_contentaccess,
    tus_inputdata,
    tus_layout,
    tus_rhui,
    tus_targetrepos,
    tus_userspacebuild
)
from leapp.libraries.common import mounting, overlaygen, repofileutils, rhsm
from leapp.libraries.stdlib import api
from leapp.models import TMPTargetRepositoriesFacts  # deprecated all the time
from leapp.models import (
    TargetOSInstallationImage,
    TargetUserSpaceInfo,
    UsedTargetRepositories,
    UsedTargetRepository
)
from leapp.utils.deprecation import suppress_deprecation


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


def _finalize_target_container(context, indata, userspace_path):
    """
    Finish content access on the freshly built target container.

    Two post-build fix-ups the build step deliberately leaves to the
    orchestrator:

    - If the target repositories were reached only through repofiles injected by
      a ``leapp-rhui-<provider>`` package, drop those repofiles. The target RHUI
      client is already installed in the container and ships the same
      definitions, so keeping the injected copies would duplicate the repos.
    - Re-enter rhsm container mode, which the build steps switched off.
    """
    if indata.rhui_info:
        api.current_logger().debug(
            'Target container should have access to content. '
            'Removing repofiles from leapp-rhui-<provider> from the target..'
        )
        setup_info = indata.rhui_info.target_client_setup_info
        if not setup_info.bootstrap_target_client:
            tus_rhui.remove_injected_repofiles(context, setup_info)

    with mounting.NspawnActions(userspace_path) as target_context:
        rhsm.set_container_mode(target_context)


@suppress_deprecation(TMPTargetRepositoriesFacts)
def _produce_facts(context, target_repoids, scratch_dir, mounts_dir):
    """
    Produce the actor's output messages.

    Parse the build (scratch) container's ``.repo`` files once and ship them as
    the point-in-time target-repositories snapshot (see CONTRACT.md for why the
    actor produces one snapshot here rather than leaving same-phase consumers to
    read the on-disk state themselves), then report which repositories were
    actually used and where the target userspace was created.
    """
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
    api.produce(UsedTargetRepositories(
        repos=[UsedTargetRepository(repoid=repo) for repo in target_repoids]))
    api.produce(TargetUserSpaceInfo(
        path=tus_layout.target_userspace_path(),
        scratch=scratch_dir,
        mounts=mounts_dir))


def perform():
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
                tus_rhui.setup_target_rhui_access_if_needed(context, indata)
                target_repoids = _gather_target_repositories(context, indata)

                userspace_path = tus_layout.target_userspace_path()
                tus_userspacebuild.build_target_userspace(
                    context, indata.packages, indata.files, target_repoids, userspace_path)

                _finalize_target_container(context, indata, userspace_path)
                _produce_facts(context, target_repoids, scratch_dir, mounts_dir)
