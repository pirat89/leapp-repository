"""
Consume and validate the actor's input messages into one typed bundle.

Single responsibility: read every message this actor depends on (except
TargetRepositories, gathered later against a live container), apply the
input-level hard-stop rules once, and expose the result as plain attributes so
the rest of the actor never calls api.consume for these messages again.
"""

from leapp.exceptions import StopActorExecution, StopActorExecutionError
from leapp.libraries.common import rhsm
from leapp.libraries.stdlib import api
from leapp.models import RequiredTargetUserspacePackages  # deprecated
from leapp.models import (
    CustomTargetRepositoryFile,
    RHSMInfo,
    RHUIInfo,
    StorageInfo,
    TargetUserSpacePreupgradeTasks,
    XFSPresence
)
from leapp.utils.deprecation import suppress_deprecation


class InputData:
    """
    Snapshot of the messages consumed by the actor.

    Attributes:
        packages (set): RPM names to install into the target userspace.
        files (list): unique CopyFile tasks to copy into the target userspace.
        rhsm_info (RHSMInfo|None): source-system RHSM data (None when skip_rhsm).
        rhui_info (RHUIInfo|None): cloud RHUI data, present only on RHUI systems.
        custom_repofiles (list): user-supplied CustomTargetRepositoryFile msgs.
        xfs_info (XFSPresence): XFS presence info (empty default when absent).
        storage_info (StorageInfo): source-system storage info (required).

    Hard stops (raised from the constructor):
        - RHSMInfo missing while rhsm is not skipped (system not registered).
        - RHSMInfo present while rhsm is skipped (contradictory producers).
        - StorageInfo missing.
    """

    def __init__(self):
        self._consume_data()

    @suppress_deprecation(RequiredTargetUserspacePackages)
    def _consume_data(self):
        """
        Wrapper function to consume majority input data.

        It doesn't consume TargetRepositories, which are consumed in the
        own function.
        """
        self.packages = {'dnf', 'dnf-command(config-manager)', 'dnf-command(download)', 'util-linux'}
        self.files = []
        _cftuples = set()

        def _update_files(copy_files):
            # add just uniq CopyFile objects to omit duplicate copying of files
            for cfile in copy_files:
                cftuple = (cfile.src, cfile.dst)
                if cftuple not in _cftuples:
                    _cftuples.add(cftuple)
                    self.files.append(cfile)

        for task in api.consume(TargetUserSpacePreupgradeTasks):
            self.packages.update(task.install_rpms)
            _update_files(task.copy_files)

        for message in api.consume(RequiredTargetUserspacePackages):
            self.packages.update(message.packages)

        # Get the RHSM information (available repos, attached SKUs, etc.) of the source system
        self.rhsm_info = next(api.consume(RHSMInfo), None)
        self.rhui_info = next(api.consume(RHUIInfo), None)
        if not self.rhsm_info and not rhsm.skip_rhsm():
            api.current_logger().warning('Could not receive RHSM information - Is this system registered?')
            raise StopActorExecution()
        if rhsm.skip_rhsm() and self.rhsm_info:
            # this should not happen. if so, raise an error as something in
            # other actors is wrong really
            raise StopActorExecutionError("RHSM is not handled but the RHSMInfo message has been produced.")

        self.custom_repofiles = list(api.consume(CustomTargetRepositoryFile))
        self.xfs_info = next(api.consume(XFSPresence), XFSPresence())
        self.storage_info = next(api.consume(StorageInfo), None)
        if not self.storage_info:
            raise StopActorExecutionError('No storage info available cannot proceed.')
