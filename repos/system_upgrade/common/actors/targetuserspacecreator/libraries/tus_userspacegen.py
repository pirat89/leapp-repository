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
from leapp.libraries.common.config import get_env
from leapp.libraries.stdlib import api
from leapp.models import TMPTargetRepositoriesFacts  # deprecated all the time
from leapp.models import (
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

                userspace_path = tus_layout.target_userspace_path()
                tus_userspacebuild.build_target_userspace(
                    context, indata.packages, indata.files, target_repoids, userspace_path)

                # If we used only repofiles from leapp-rhui-<provider> then remove these as they
                # provide duplicit definitions as the target clients already installed in the
                # target container
                if indata.rhui_info:
                    api.current_logger().debug(
                        'Target container should have access to content. '
                        'Removing repofiles from leapp-rhui-<provider> from the target..'
                    )
                    setup_info = indata.rhui_info.target_client_setup_info
                    if not setup_info.bootstrap_target_client:
                        tus_rhui.remove_injected_repofiles(context, setup_info)

                # and do not forget to set the rhsm into the container mode again
                with mounting.NspawnActions(userspace_path) as target_context:
                    rhsm.set_container_mode(target_context)

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
