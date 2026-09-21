import os

from leapp import reporting
from leapp.exceptions import StopActorExecution
from leapp.libraries.actor import bootstrap, constants, inputdata, targetrepos, targetrhui
from leapp.libraries.common import mounting, overlaygen, rhsm
from leapp.libraries.common.config import get_product_type
from leapp.libraries.stdlib import api
from leapp.models import (
    RepositoriesFactsTarget,
    TargetOSInstallationImage,
    TargetUserSpaceInfo,
    UsedTargetRepositories,
    UsedTargetRepository
)

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


def _report_missing_product_cert(details):
    """
    Report the missing target product certificate as a HIGH inhibitor.

    The product certificate path is determined by :func:`rhsm.switch_certificate`,
    which raises :class:`rhsm.MissingTargetProductCertificate` carrying the
    expected path in its ``details`` when the certificate cannot be found. The
    path determination lives in the shared ``rhsm`` library, but the user-facing
    report stays here so the remediation (which points at the actor's target
    version override) is owned by the actor.

    :param details: the ``details`` dict of the caught MissingTargetProductCertificate
    """
    cert_path = details.get('cert_path')
    cert = os.path.basename(cert_path) if cert_path else 'unknown'

    additional_summary = ''
    if get_product_type('target') == 'beta':
        additional_summary = (
            ' This can happen when upgrading a beta system and the chosen target version does not have'
            ' beta certificates attached (for example, because the GA has been released already).'
        )

    reporting.create_report([
        reporting.Title('Cannot find the product certificate file for the chosen target system.'),
        reporting.Summary(
            'Expected certificate: {cert} with path {path} but it could not be found.{additional}'.format(
                cert=cert, path=cert_path, additional=additional_summary)
        ),
        reporting.Groups([reporting.Groups.REPOSITORY]),
        reporting.Groups([reporting.Groups.INHIBITOR]),
        reporting.Severity(reporting.Severity.HIGH),
        reporting.Remediation(hint=(
            'Set the corresponding target os version in the LEAPP_DEVEL_TARGET_RELEASE environment variable for'
            'which the {cert} certificate is provided'.format(cert=cert)
        )),
    ])


def perform():
    indata = inputdata.InputData()
    reserve_space = overlaygen.get_recommended_leapp_free_space(bootstrap._get_target_userspace())
    with overlaygen.create_source_overlay(
            mounts_dir=constants.MOUNTS_DIR,
            scratch_dir=constants.SCRATCH_DIR,
            storage_info=indata.storage_info,
            xfs_info=indata.xfs_info,
            scratch_reserve=reserve_space) as overlay:
        with overlay.nspawn() as context:
            # Mount the ISO into the scratch container
            target_iso = next(api.consume(TargetOSInstallationImage), None)
            with mounting.mount_upgrade_iso_to_root_dir(overlay.target, target_iso):

                # TODO: this is out of tests completely
                targetrhui.setup_target_rhui_access_if_needed(context, indata)

                try:
                    target_repoids = targetrepos._gather_target_repositories(context, indata)
                except rhsm.MissingTargetProductCertificate as e:
                    # Path determination moved into rhsm.switch_certificate; the
                    # user-facing inhibitor + soft stop are preserved here.
                    _report_missing_product_cert(e.details or {})
                    raise StopActorExecution()
                bootstrap._create_target_userspace(context, indata, indata.packages, indata.files, target_repoids)
                # TODO: this is tmp solution as proper one needs significant refactoring
                target_repo_facts = targetrepos.parsed_repofiles_or_stop(
                    context,
                    "Failed to parse target system repofiles: {}",
                    'Ensure the repository definition is correct or remove it '
                    'if the repository is not needed anymore. '
                    'This issue is typically caused by missing definition of the name field. '
                    'For more information, see: https://access.redhat.com/solutions/6969001.')
                api.produce(RepositoriesFactsTarget(repositories=target_repo_facts))
                # ## TODO ends here
                api.produce(UsedTargetRepositories(
                    repos=[UsedTargetRepository(repoid=repo) for repo in target_repoids]))
                api.produce(TargetUserSpaceInfo(
                    path=bootstrap._get_target_userspace(),
                    scratch=constants.SCRATCH_DIR,
                    mounts=constants.MOUNTS_DIR))
