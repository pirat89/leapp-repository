from leapp.libraries.stdlib import api
from leapp.libraries.common.config import architecture
from leapp.models import SapHanaInfo
from leapp import reporting


# SAP HANA Compatibility

SAP_HANA_MINIMAL_MAJOR_VERSION = 2
SAP_HANA_MINIMAL_SP = 5
SAP_HANA_MINIMAL_REV = 52
SAP_HANA_MINIMAL_VERSION_STRING = 'HANA 2.0 SPS05 rev 52'


def running_check(info):
    if info.running:
        reporting.create_report([
            reporting.Title('Found running SAP HANA instances'),
            reporting.Summary(
                'In order to perform a system upgrade it is necessary that all instances of SAP Hana are stopped.'
                'Please shutdown all SAP Hana instances before you continue with the upgrade.'
            ),
            reporting.Severity(reporting.Severity.HIGH),
            reporting.Tags([reporting.Tags.SANITY]),
            reporting.Flags([reporting.Flags.INHIBITOR]),
            reporting.Audience('sysadmin')
        ])


def _add_hana_details(target, instance):
    target.setdefault(instance.name, {'numbers': set(), 'path': instance.path, 'admin': instance.admin})
    target[instance.name]['numbers'].add(instance.instance_number)


def _create_detected_instances_list(details):
    result = []
    for name, meta in details.items():
        result.append(('Name: {name}\n'
                       '  Instances: {instances}\n'
                       '  Admin: {admin}\n'
                       '  Path: {path}').format(name=name,
                                                instances=', '.join(meta['numbers']),
                                                admin=meta['admin'],
                                                path=meta['path']))
    if result:
        return '- {}'.format('\n- '.join(result))
    return ''


def version1_check(info):
    found = {}
    for instance in info.instances:
        if instance.manifest.get('release', None) == '1.00':
            _add_hana_details(found, instance)

    if found:
        detected = _create_detected_instances_list(found)
        reporting.create_report([
            reporting.Title('Found outdated SAP HANA 1'),
            reporting.Summary(
                ('SAP Hana 1.00 is not supported on the version of RHEL you are'
                 ' upgrading to. In order to upgrade RHEL, you will have to'
                 ' upgrade SAP Hana first to the latest version of SAP Hana 2.00.\n\n'
                 'The following instances have been detected to be running version 1.00:\n'
                 '{}'.format(detected))
            ),
            reporting.Severity(reporting.Severity.HIGH),
            reporting.Tags([reporting.Tags.SANITY]),
            reporting.Flags([reporting.Flags.INHIBITOR]),
            reporting.Audience('sysadmin')
        ])


def _fullfills_hana_min_version(instance):
    release = instance.manifest.get('release', '0.00')
    parts = release.split('.')
    try:
        if int(parts[0]) != SAP_HANA_MINIMAL_MAJOR_VERSION:
            api.current_logger().info('Unsupported major version {} for instance {}'.format(release, instance.name))
            return False
    except (ValueError, IndexError):
        api.current_logger().warn(
            'Failed to parse manifest release field for instance {}'.format(instance.name), exc_info=True)
        return False

    number = instance.manifest.get('rev-number', '000')
    if len(number) > 2 and number.isdigit():
        sp = number[0:2].lstrip('0')
        if not sp or int(sp) < SAP_HANA_MINIMAL_SP:
            return False
        rev = number.lstrip('0')
        if not rev or int(rev) < SAP_HANA_MINIMAL_REV:
            return False
    else:
        api.current_logger().warn(
            'Invalid rev-number field value `{}` in manifest for instance {}'.format(number, instance.name))
        return False
    return True


def version2_check(info):
    found = {}
    for instance in info.instances:
        if instance.manifest.get('release', None) == '1.00':
            continue
        if not _fullfills_hana_min_version(instance):
            _add_hana_details(found, instance)

    if found:
        detected = _create_detected_instances_list(found)
        reporting.create_report([
            reporting.Title('SAP Hana needs to be updated before upgrade'),
            reporting.Summary(
                ('Please update SAP Hana to at least {min_hana_version} to continue the upgrade.\n\n'
                 'The following SAP Hana instances have been detected to be running with a lower version'
                 ' than required on the target system:\n'
                 '{detected}').format(detected=detected, min_hana_version=SAP_HANA_MINIMAL_VERSION_STRING)
            ),
            reporting.Severity(reporting.Severity.HIGH),
            reporting.Tags([reporting.Tags.SANITY]),
            reporting.Flags([reporting.Flags.INHIBITOR]),
            reporting.Audience('sysadmin')
        ])


def platform_check():
    if api.current_actor().configuration.flavour != 'saphana':
        return False

    if not architecture.matches_architecture(architecture.ARCH_X86_64):
        reporting.create_report([
            reporting.Title('SAP Hana upgrades are only supported on X86_64 systems'),
            reporting.Summary(
                ('Upgrades for SAP Hana are only supported on X86_64 systems.'
                 ' For more information please regard to the documentation.')
            ),
            reporting.Severity(reporting.Severity.HIGH),
            reporting.Tags([reporting.Tags.SANITY]),
            reporting.Flags([reporting.Flags.INHIBITOR]),
            reporting.Audience('sysadmin'),
            reporting.ExternalLink(
                url='https://access.redhat.com/solutions/5533441',
                title='How do I upgrade from Red Hat Enterprise Linux 7 to Red Hat Enterprise Linux 8 with SAP HANA')
        ])
        return False

    return True


def perform_check():
    if not platform_check():
        return

    info = next(api.consume(SapHanaInfo), None)
    if not info:
        return

    running_check(info)
    version1_check(info)
    version2_check(info)
