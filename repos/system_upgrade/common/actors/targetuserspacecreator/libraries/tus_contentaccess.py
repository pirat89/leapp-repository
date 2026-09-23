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

from leapp.exceptions import StopActorExecutionError
from leapp.libraries.common import rhsm
from leapp.libraries.common.config import get_target_distro_id
from leapp.libraries.common.config.version import get_target_major_version


def prepare_repository_access(context, indata, prod_cert_path):
    """
    Make the target repositories reachable from inside the container.

    Switch the product certificate and enable RHSM container mode, adjust the
    CentOS Stream ``$stream`` variable when the target is CentOS Stream, and
    install the custom repofiles supplied on the command line.

    :param context: the scratch container to set up
    :type context: mounting.IsolatedActions class
    :param indata: majority of input data for the actor
    :type indata: inputdata.InputData
    :param prod_cert_path: path where the target product cert is stored
    :type prod_cert_path: string
    """
    rhsm.set_container_mode(context)
    rhsm.switch_certificate(context, indata.rhsm_info, prod_cert_path)

    if get_target_distro_id() == 'centos':
        adjust_dnf_stream_variable(context)

    _install_custom_repofiles(context, indata.custom_repofiles)


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
