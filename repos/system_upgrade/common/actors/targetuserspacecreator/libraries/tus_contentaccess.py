"""
Establish non-RHUI content access inside the target-userspace container.

Single responsibility: make the target repositories reachable from inside the
scratch container before repository discovery runs - switch the RHSM
certificate, put RHSM into container mode, point the CentOS Stream ``$stream``
variable at the target major version, and install any user-supplied custom
repofiles.

This is the *command* half of what used to be a single discovery seam. Keeping
it separate from repository discovery (the *query* half, in ``targetrepos``) is
deliberate command/query separation: discovery must not mutate the container.
"""

import os

from leapp import reporting
from leapp.exceptions import StopActorExecution, StopActorExecutionError
from leapp.libraries.common import rhsm
from leapp.libraries.common.config import get_product_type, get_target_distro_id
from leapp.libraries.common.config.version import get_target_major_version


def prepare_repository_access(context, indata):
    """
    Make the target repositories reachable from inside the container.

    Switch the product certificate and enable RHSM container mode, adjust the
    CentOS Stream ``$stream`` variable when the target is CentOS Stream, and
    install the custom repofiles supplied on the command line.

    The target product certificate is discovered automatically by
    :func:`rhsm.switch_certificate`; when neither the minor- nor the
    major-version certificate can be found we turn the resulting
    :class:`rhsm.MissingTargetProductCertificate` into the missing-cert
    upgrade inhibitor.

    :param context: the scratch container to set up
    :type context: mounting.IsolatedActions class
    :param indata: majority of input data for the actor
    :type indata: inputdata.InputData
    """
    rhsm.set_container_mode(context)
    try:
        rhsm.switch_certificate(context, indata.rhsm_info)
    except rhsm.MissingTargetProductCertificate as exc:
        _inhibit_missing_product_certificate(exc)

    if get_target_distro_id() == 'centos':
        adjust_dnf_stream_variable(context)

    _install_custom_repofiles(context, indata.custom_repofiles)


def _inhibit_missing_product_certificate(exc):
    """
    Report the missing target product certificate as an upgrade inhibitor.

    :func:`rhsm.switch_certificate` auto-discovers the target product
    certificate and raises :class:`rhsm.MissingTargetProductCertificate` when
    neither the minor- nor the major-version certificate exists. Surface that as
    an inhibitor with a remediation hint rather than a bare actor error.

    :param exc: the exception raised by :func:`rhsm.switch_certificate`
    :type exc: rhsm.MissingTargetProductCertificate
    """
    cert_path = (exc.details or {}).get('cert_path')
    cert = os.path.basename(cert_path) if cert_path else 'the required product certificate'

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
    raise StopActorExecution()


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
