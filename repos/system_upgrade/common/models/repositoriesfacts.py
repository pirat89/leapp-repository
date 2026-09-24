from leapp.models import fields, Model
from leapp.topics import SystemFactsTopic


class RepositoryData(Model):
    topic = SystemFactsTopic

    repoid = fields.String()
    name = fields.String()
    baseurl = fields.Nullable(fields.String())
    metalink = fields.Nullable(fields.String())
    mirrorlist = fields.Nullable(fields.String())
    enabled = fields.Boolean(default=True)
    additional_fields = fields.Nullable(fields.String())
    proxy = fields.Nullable(fields.String())


class RepositoryFile(Model):
    topic = SystemFactsTopic

    file = fields.String()
    data = fields.List(fields.Model(RepositoryData))


class RepositoriesFacts(Model):
    topic = SystemFactsTopic

    repositories = fields.List(fields.Model(RepositoryFile))


class TargetRepositoriesFacts(RepositoriesFacts):
    """Snapshot of the ``.repo`` files present in the built target userspace.

    Produced once by the ``target_userspace_creator`` actor in the
    TargetTransactionFacts phase, right after the target userspace is built and
    before any later phase rewrites its repofiles. Both consumers run later, in
    the TargetTransactionChecks phase -- ``adjust_local_repos`` (which rewrites
    local ``file://`` URLs in the repofiles in place) and
    ``missing_gpg_keys_inhibitor`` -- so shipping a single pre-mutation snapshot
    gives them a stable view that does not depend on actor ordering or on
    re-reading the on-disk repofiles.

    Deliberately a distinct type from the source-system ``RepositoriesFacts``
    (same field shape, different subject and lifecycle) to keep target and source
    repository facts from being conflated. Internal to the in-place upgrade
    workflow; not a stable interface for out-of-tree consumers.
    """
