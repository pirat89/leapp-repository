import dnf

from leapp.exceptions import StopActorExecutionError


def get_package_repository_data():
    """ Return dictionary mapping package name with repository from which it was installed.
    Note:
        There's no yum module for py3. The dnf module could have been used
        instead but there's a bug in dnf preventing us to do so:
        https://bugzilla.redhat.com/show_bug.cgi?id=1789840
    """
    base = dnf.Base()
    base.fill_sack()
    query = base.sack.query()
    installed_pkgs = query.installed()
    pkg_repos = {}

    try:
        for pkg in installed_pkgs:
            pkg_repos[pkg.name] = pkg.ui_from_repo.lstrip('@')
    except ValueError as e:
        if 'locale' not in str(e):  # reraise if error is not related to locales
            raise e
        raise StopActorExecutionError(
            message='Failed to get installed RPM packages because of an invalid locale',
            details={
                'hint': 'Please run leapp with a valid locale. ' +
                        'You can get a list of installed locales by running `locale -a`.'
            })

    return pkg_repos
