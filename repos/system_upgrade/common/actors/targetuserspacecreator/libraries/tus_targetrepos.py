"""
Target repository discovery and selection.

Single responsibility: discover which repoids are reachable from inside the
already-prepared scratch container - those provided by the distribution, by the
RHUI target clients, and everything defined in the container - then select the
subset the upgrade requires, inhibiting the upgrade when the required
repositories are missing, ambiguous, or absent.

This is the *query* half of the old discovery seam and performs no container
mutation of its own; the *command* half that prepares access lives in
``contentaccess`` (and ``rhui`` for cloud systems). It depends on ``rhui`` only
to read the repoids the target RHUI clients provide.
"""

from leapp import reporting
from leapp.exceptions import StopActorExecution, StopActorExecutionError
from leapp.libraries.actor import tus_rhui
from leapp.libraries.common import distro, repofileutils, rhsm
from leapp.libraries.common.config import get_source_distro_id, get_target_distro_id, is_conversion
from leapp.libraries.common.config.version import get_source_major_version, get_target_major_version
from leapp.libraries.stdlib import api, format_list
from leapp.models import RHELTargetRepository, TargetRepositories
from leapp.utils.deprecation import suppress_deprecation


@suppress_deprecation(RHELTargetRepository)  # member of TargetRepositories
def select_target_repositories(context, indata):
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
                # NOTE: We shall report that the RHEL repos that we deem necessary for
                # the upgrade are not available; but currently it would just print bunch of
                # data every time as we maps EUS and other repositories as well. But these
                # do not have to be necessary available on the target system in the time
                # of the upgrade. Let's skip it for now until it's clear how we will deal
                # with it. (Tracked out-of-scope feature gap, not a TODO.)
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
        _report_no_enabled_target_repos()
    if missing_custom_repoids:
        _report_missing_custom_repos(missing_custom_repoids)
    return target_repoids


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
        rhui_repoids = tus_rhui.get_rhui_available_repoids(context, indata.rhui_info)
        distro_repoids.extend(rhui_repoids)

    return set(distro_repoids)


def _inhibit_if_no_base_repos(distro_repoids):
    # NOTE: strengthening this beyond the minimal baseos+appstream heuristic below
    # (verifying every required target repo ID is available) is a deliberate,
    # behaviour-preserving feature gap - a generic solution is still an open design
    # question (see the note just below). Tracked out-of-scope, not a TODO.
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


def _get_all_available_repoids(context):
    try:
        repofiles = repofileutils.get_parsed_repofiles(context)
    except repofileutils.InvalidRepoDefinition as e:
        raise StopActorExecutionError(
            message="Failed to parse available repoids: {}".format(str(e)),
            details={
                'hint': 'Ensure the repository definition is correct or remove it '
                        'if the repository is not required for the upgrade.'
            })
    # NOTE: when rhsm is skipped, the rhsm code path that flags duplicate repoids
    # is not taken, so the duplicate-repo inhibitor is run explicitly here instead.
    # Unifying this with rhsm's copy is an out-of-scope shared-lib change tracked in
    # issue #486 (see _inhibit_on_duplicate_repos below). Not a TODO.
    if rhsm.skip_rhsm():
        _inhibit_on_duplicate_repos(repofiles)
    repoids = []
    for rfile in repofiles:
        if rfile.data:
            repoids += [repo.repoid for repo in rfile.data]
    return set(repoids)


def _inhibit_on_duplicate_repos(repofiles):
    """
    Inhibit the upgrade if any repoid is defined multiple times.

    When that happens, it not only shows misconfigured system, but then
    we can't get details of all the available repos as well.
    """
    # NOTE: this intentionally mirrors rhsm._inhibit_on_duplicate_repos but with an
    # upgrade-container-specific report summary. De-duplicating the two would require
    # parameterizing the shared rhsm helper's message, an out-of-scope shared-lib
    # change tracked in issue #486. Not a TODO.
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


def _report_no_enabled_target_repos():
    """
    Report the no-enabled-target-repositories inhibitor and stop the actor.

    Emitted when repository selection yields no enabled target repositories -
    typically an unregistered system, or ``--no-rhsm`` used without any custom
    repositories being provided.
    """
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


def _report_missing_custom_repos(missing_custom_repoids):
    """
    Report the missing-custom-repositories inhibitor and stop the actor.

    Emitted when a required custom target repository was requested but could not
    be found among the repositories available inside the container.
    """
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
