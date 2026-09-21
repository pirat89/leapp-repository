import os

from leapp import reporting
from leapp.exceptions import StopActorExecution, StopActorExecutionError
from leapp.libraries.actor import targetrhui
from leapp.libraries.common import distro, repofileutils, rhsm
from leapp.libraries.common.config import get_source_distro_id, get_target_distro_id, is_conversion
from leapp.libraries.common.config.version import get_source_major_version, get_target_major_version
from leapp.libraries.stdlib import api, format_list
from leapp.models import RHELTargetRepository, TargetRepositories
from leapp.utils.deprecation import suppress_deprecation


def parsed_repofiles_or_stop(context, error_message, hint):
    """
    Parse the repofiles present in the given container context.

    Thin wrapper around :func:`repofileutils.get_parsed_repofiles` that turns a
    malformed repository definition into a StopActorExecutionError carrying the
    caller-provided message and remediation hint. The message and hint differ per
    call site (different container states / audiences), so they are passed in
    rather than hardcoded here.

    :param context: the container whose /etc/yum.repos.d is parsed
    :type context: mounting.IsolatedActions
    :param error_message: message template with a single ``{}`` for the error detail
    :type error_message: str
    :param hint: remediation hint stored in the error details
    :type hint: str
    :return: parsed repofiles
    :rtype: list[RepositoryFile]
    """
    try:
        return repofileutils.get_parsed_repofiles(context)
    except repofileutils.InvalidRepoDefinition as e:
        raise StopActorExecutionError(
            message=error_message.format(str(e)),
            details={'hint': hint})


def _inhibit_on_duplicate_repos(repofiles):
    """
    Inhibit the upgrade if any repoid is defined multiple times.

    When that happens, it not only shows misconfigured system, but then
    we can't get details of all the available repos as well.
    """
    # TODO: this is is duplicate of rhsm._inhibit_on_duplicate_repos
    # Issue: #486
    duplicates = repofileutils.get_duplicate_repositories(repofiles).keys()

    if not duplicates:
        return
    api.current_logger().warning(
        'The following repoids are defined multiple times:{}'
        .format(format_list(duplicates))
    )

    reporting.create_report([
        reporting.Title('A YUM/DNF repository defined multiple times'),
        reporting.Summary(
            'The following repositories are defined multiple times inside the'
            ' "upgrade" container:{}'
            .format(format_list(duplicates))
        ),
        reporting.Severity(reporting.Severity.MEDIUM),
        reporting.Groups([reporting.Groups.REPOSITORY]),
        reporting.Groups([reporting.Groups.INHIBITOR]),
        reporting.Remediation(hint=(
            'Remove the duplicate repository definitions or change repoids of'
            ' conflicting repositories on the system to prevent the'
            ' conflict.'
            )
        )
    ])


def _get_all_available_repoids(context):
    repofiles = parsed_repofiles_or_stop(
        context,
        "Failed to parse available repoids: {}",
        'Ensure the repository definition is correct or remove it '
        'if the repository is not required for the upgrade.')
    # TODO: this is not good solution, but keep it as it is now
    # Issue: #486
    if rhsm.skip_rhsm():
        # only if rhsm is skipped, the duplicate repos are not detected
        # automatically and we need to do it extra
        _inhibit_on_duplicate_repos(repofiles)
    repoids = []
    for rfile in repofiles:
        if rfile.data:
            repoids += [repo.repoid for repo in rfile.data]
    return set(repoids)


def _inhibit_if_no_base_repos(distro_repoids):
    # FIXME: check that required repo IDs (baseos, appstream)
    # + or check that all required RHEL repo IDs are available.

    target_major_version = get_target_major_version()
    # NOTE(ivasilev) For the moment at least AppStream and BaseOS repos are required. While we are still
    # contemplating on what can be a generic solution to checking this, let's introduce a minimal check for
    # at-least-one-appstream and at-least-one-baseos among present repoids
    no_baseos = all("baseos" not in ri for ri in distro_repoids)
    no_appstream = all("appstream" not in ri for ri in distro_repoids)
    if no_baseos or no_appstream:
        report = [
            reporting.Title('Cannot find required basic target OS repositories.'),
            reporting.Summary(
                'This can happen when a repository ID was entered incorrectly either while using the --enablerepo'
                ' option of leapp or in a third party actor that produces a CustomTargetRepositoryMessage.'
            ),
            reporting.Groups([reporting.Groups.REPOSITORY]),
            reporting.Severity(reporting.Severity.HIGH),
            reporting.Groups([reporting.Groups.INHIBITOR]),
            reporting.ExternalLink(
                url='https://access.redhat.com/solutions/5392811',
                title='RHEL 7 to RHEL 8 LEAPP Upgrade Failing When Using Red Hat Satellite'
            ),
            reporting.ExternalLink(
                # https://red.ht/preparing-for-upgrade-to-rhel8
                # https://red.ht/preparing-for-upgrade-to-rhel9
                # https://red.ht/preparing-for-upgrade-to-rhel10
                url='https://red.ht/preparing-for-upgrade-to-rhel{}'.format(target_major_version),
                title='Preparing for the upgrade'),
            reporting.Key('f5770a56e540f27d370da7b697cb4a2e81e2c30d'),
        ]
        if get_target_distro_id() == 'rhel':
            report.append(reporting.Remediation(hint=(
                'It is required to have RHEL repositories on the system'
                ' provided by the subscription-manager unless the --no-rhsm'
                ' option is specified. You might be missing a valid SKU for'
                ' the target system or have a failed network connection.'
                ' Check whether your system is attached to a valid SKU that is'
                ' providing RHEL {} repositories.'
                ' If you are using Red Hat Satellite, read the upgrade documentation'
                ' to set up Satellite and the system properly.'
                .format(target_major_version)))
            )
        reporting.create_report(report)
        raise StopActorExecution()


def _get_distro_available_repoids(context, indata):
    """
    Get repoids provided by the distribution

    On RHEL: RH repositories are provided either by RHSM or are stored in the
             expected repo file provided by RHUI special packages (every cloud
             provider has itw own rpm).
    On other: Repositories are provided in specific repofiles (e.g. centos.repo
              and centos-addons.repo on CS)
              Exception: On CS8->CS9 there are no distro-provided repoids as
              the repofile layout and urls are different
    Conversions: Only custom repos - no distro repoids (all distros)

    :return: A set of repoids provided by distribution
    :rtype: set[str]
    """
    distro_repoids = distro.get_target_distro_repoids(context)
    target_distro = get_target_distro_id()
    rhel_and_rhsm = target_distro == 'rhel' and not rhsm.skip_rhsm()
    is_source_cs8 = (
        get_source_distro_id() == "centos" and get_source_major_version() == '8'
    )

    if (
        not is_conversion()  # conversions only work with custom repos
        and not is_source_cs8  # there are no distro_repoids on CS8->CS9
        and (target_distro != "rhel" or rhel_and_rhsm)
    ):
        _inhibit_if_no_base_repos(distro_repoids)

    if indata and indata.rhui_info:
        rhui_repoids = targetrhui._get_rhui_available_repoids(context, indata.rhui_info)
        distro_repoids.extend(rhui_repoids)

    return set(distro_repoids)


@suppress_deprecation(RHELTargetRepository)  # member of TargetRepositories
def gather_target_repositories(context, indata):
    """
    Get available required target repositories and inhibit or raise error if basic checks do not pass.

    In case of repositories provided by Red Hat, it's checked whether the basic
    required repositories are available (or at least defined) in the given
    context. If not, raise StopActorExecutionError.

    For the custom target repositories we expect all of them have to be defined.
    If any custom target repository is missing, raise StopActorExecutionError.

    If any repository is defined multiple times, produce the inhibitor Report
    msg.

    :param context: An instance of a mounting.IsolatedActions class
    :type context: mounting.IsolatedActions class
    :return: List of target system repoids
    :rtype: set[str]
    """

    distro_repoids = _get_distro_available_repoids(context, indata)
    if distro_repoids:
        api.current_logger().info(
            "The following repoids are considered as provided by the '{}' distribution:{}".format(
                get_target_distro_id(),
                format_list(distro_repoids),
            )
        )
    else:
        api.current_logger().warning(
            "No repoids provided by the {} distribution have been discovered".format(
                get_target_distro_id()
            )
        )

    all_repoids = _get_all_available_repoids(context)

    target_repoids = set()
    missing_custom_repoids = set()
    for target_repo in api.consume(TargetRepositories):
        for distro_repo in target_repo.distro_repos:
            if distro_repo.repoid in distro_repoids:
                target_repoids.add(distro_repo.repoid)
            else:
                # TODO: We shall report that the RHEL repos that we deem necessary for
                # the upgrade are not available; but currently it would just print bunch of
                # data every time as we maps EUS and other repositories as well. But these
                # do not have to be necessary available on the target system in the time
                # of the upgrade. Let's skip it for now until it's clear how we will deal
                # with it.
                pass

        for custom_repo in target_repo.custom_repos:
            if custom_repo.repoid in all_repoids:
                target_repoids.add(custom_repo.repoid)
            else:
                missing_custom_repoids.add(custom_repo.repoid)
    api.current_logger().debug(
        "Gathered target repositories: {}".format(", ".join(sorted(target_repoids)))
    )

    if not target_repoids:
        target_major_version = get_target_major_version()
        reporting.create_report([
            reporting.Title('There are no enabled target repositories'),
            reporting.Summary(
                'This can happen when a system is not correctly registered with the subscription manager'
                ' or, when the leapp --no-rhsm option has been used, no custom repositories have been'
                ' passed on the command line.'
            ),
            reporting.Groups([reporting.Groups.REPOSITORY]),
            reporting.Groups([reporting.Groups.INHIBITOR]),
            reporting.Severity(reporting.Severity.HIGH),
            reporting.Remediation(hint=(
                'Ensure the system is correctly registered with the subscription manager and that'
                ' the current subscription is entitled to install the requested target version {version}.'
                ' If you used the --no-rhsm option (or the LEAPP_NO_RHSM=1 environment variable is set),'
                ' ensure the custom repository file is provided with'
                ' properly defined repositories and that the --enablerepo option for leapp is set if the'
                ' repositories are defined in any repofiles under the /etc/yum.repos.d/ directory.'
                ' For more information on custom repository files, see the documentation.'
                ' Finally, verify that the "/etc/leapp/files/repomap.json" file is up-to-date.'
            ).format(version=api.current_actor().configuration.version.target)),
            reporting.ExternalLink(
                # https://red.ht/preparing-for-upgrade-to-rhel8
                # https://red.ht/preparing-for-upgrade-to-rhel9
                # https://red.ht/preparing-for-upgrade-to-rhel10
                url='https://red.ht/preparing-for-upgrade-to-rhel{}'.format(target_major_version),
                title='Preparing for the upgrade'),
            reporting.ExternalLink(
                url='https://access.redhat.com/solutions/7001181',
                title='LEAPP Upgrade Failing from RHEL 7 to RHEL 8 when system is '
                      'registered to custromer portal'
            ),
            reporting.RelatedResource("file", "/etc/leapp/files/repomap.json"),
            reporting.RelatedResource("file", "/etc/yum.repos.d/")
        ])
        raise StopActorExecution()
    if missing_custom_repoids:
        reporting.create_report([
            reporting.Title('Some required custom target repositories have not been found'),
            reporting.Summary(
                'This can happen when a repository ID was entered incorrectly either'
                ' while using the --enablerepo option of leapp, or in a third party actor that produces a'
                ' CustomTargetRepositoryMessage.\n'
                'The following repositories IDs could not be found in the target configuration:{}'
                .format(format_list(missing_custom_repoids))
            ),
            reporting.Groups([reporting.Groups.REPOSITORY]),
            reporting.Groups([reporting.Groups.INHIBITOR]),
            reporting.Severity(reporting.Severity.HIGH),
            reporting.ExternalLink(
                # NOTE: Article covers both RHEL 7 to RHEL 8 and RHEL 8 to RHEL 9
                url='https://access.redhat.com/articles/4977891',
                title='Customizing your Red Hat Enterprise Linux in-place upgrade'),
            reporting.Remediation(hint=(
                'Consider using the custom repository file, which is documented in the official'
                ' upgrade documentation. Check whether a repository ID has been'
                ' entered incorrectly with the --enablerepo option of leapp.'
                ' Check the leapp logs to see the list of all available repositories.'
            ))
        ])
        raise StopActorExecution()
    return target_repoids


def _install_custom_repofiles(context, custom_repofiles):
    """
    Install the required custom repository files into the container.

    The repository files are copied from the host into the /etc/yum.repos.d
    directory into the container.

    :param context: the container where the repofiles should be copied
    :type context: mounting.IsolatedActions class
    :param custom_repofiles: list of custom repo files
    :type custom_repofiles: List(CustomTargetRepositoryFile)
    """
    for rfile in custom_repofiles:
        _dst_path = os.path.join('/etc/yum.repos.d', os.path.basename(rfile.file))
        context.copy_to(rfile.file, _dst_path)


def adjust_dnf_stream_variable(context, varfile='/etc/dnf/vars/stream'):
    """
    Adjust the version in the dnf 'stream' variable to the target version.

    URLs in CentOS Stream repofiles contain the $stream variable which,
    if not adjusted, retains the value from the source system making
    the URLs point to repos for the source version. This function adjusts
    the variable so that the URLs point to the target version repos.
    """

    target_version = get_target_major_version()
    try:
        with context.open(varfile, 'w') as f:
            f.write(target_version + '-stream\n')
    except (FileNotFoundError, OSError) as e:
        raise StopActorExecutionError(
            message='Failed to adjust dnf variable in {} to "{}".'.format(varfile, target_version + '-stream'),
            details={'details': str(e)})


def _gather_target_repositories(context, indata):
    """
    This is wrapper function to gather the target repoids.

    Probably the function could be partially merged into gather_target_repositories
    and this could be really just wrapper with the switch of certificates.
    I am keeping that for now as it is as interim step.

    The target product certificate path is determined automatically by
    :func:`rhsm.switch_certificate`; if it cannot be found the function raises
    :class:`rhsm.MissingTargetProductCertificate`, which the caller translates
    into the user-facing inhibitor report.

    :param context: the container where the repofiles should be copied
    :type context: mounting.IsolatedActions class
    :param indata: majority of input data for the actor
    :type indata: class InputData
    :raises rhsm.MissingTargetProductCertificate: if the product cert is missing
    """
    rhsm.set_container_mode(context)
    rhsm.switch_certificate(context, indata.rhsm_info)

    if get_target_distro_id() == 'centos':
        adjust_dnf_stream_variable(context)

    _install_custom_repofiles(context, indata.custom_repofiles)
    return gather_target_repositories(context, indata)
