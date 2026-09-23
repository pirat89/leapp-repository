"""
Filesystem layout of the target userspace.

Single responsibility: decide where the target userspace lives and provision
its directories. This is a leaf module - it must not import any other actor
library, so modules that need the userspace location can depend on it without
creating import cycles.
"""

from leapp.exceptions import StopActorExecutionError
from leapp.libraries.common import utils
from leapp.libraries.common.config.version import get_target_major_version
from leapp.libraries.stdlib import api


def target_userspace_path():
    """Return the path where the target userspace is built."""
    return '/var/lib/leapp/el{}userspace'.format(get_target_major_version())


def create_target_userspace_directories(target_userspace):
    api.current_logger().debug('Creating target userspace directories.')
    try:
        utils.makedirs(target_userspace)
        api.current_logger().debug('Done creating target userspace directories.')
    except OSError:
        api.current_logger().error(
            'Failed to create temporary target userspace directories %s', target_userspace, exc_info=True)
        # This is an attempt for giving the user a chance to resolve it on their own
        raise StopActorExecutionError(
            message='Failed to prepare environment for package download while creating directories.',
            details={
                'hint': 'Please ensure that {directory} is empty and modifiable.'.format(directory=target_userspace)
            }
        )
