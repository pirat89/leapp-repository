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
