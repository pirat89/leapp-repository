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


class RepositoriesFactsTarget(Model):
    """
    Point-in-time snapshot of the .repo files present in the created target
    userspace container, captured immediately after its creation.

    The data is read from the container's repofiles (which, in RHUI cases, may
    differ from the bundled target-OS repos). It is retained mainly for
    auditing / post-mortem purposes and MAY not reflect later .repo edits
    (e.g. done by the adjustlocalrepos actor); consumers that need the current
    on-disk state must read it from the path in TargetUserSpaceInfo.

    This is an independent model from RepositoriesFacts (which describes the
    *source* system) even though it shares the same shape - the two evolve
    independently.
    """
    topic = SystemFactsTopic

    repositories = fields.List(fields.Model(RepositoryFile))
