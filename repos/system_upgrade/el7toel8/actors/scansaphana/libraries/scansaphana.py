from os import path, listdir

from leapp.libraries.stdlib import CalledProcessError, api, run
from leapp.models import SapHanaInfo, SapHanaInstanceInfo

HANA_BASE_PATH = '/hana/shared'
HANA_MANIFEST_PATH = 'exe/linuxx86_64/hdb/manifest'
HANA_SAPCONTROL_PATH = 'exe/linuxx86_64/hdb/sapcontrol'


def perform_sap_hana_scan():
    """
    Produces a message with details collected around SAP HANA.
    """

    api.produce(search_sap_hana_instances())


def parse_manifest(path):
    """ Parses a SAP HANA manifest into a dictionary """
    def _decoded(s):
        if hasattr(s, 'decode'):
            return s.decode('utf-8')
        return s

    data = {}
    try:
        with open(path, 'r') as f:
            for line in _decoded(f.read()).split('\n'):
                key, value = line.split(':', 1)
                data[key] = value.strip()
    except OSError:
        return None
    return data


def search_sap_hana_instances():
    """
    Searches for all instances of SAP HANA on the system and gets the information for it

    This code will go through all entries in /hana/shared and checks for all instances within.
    For each instance it will check the status and record the location.

    :return: SapHanaInfo
    """
    result = []
    any_running = False
    installed = False
    if path.isdir(HANA_BASE_PATH):
        for entry in listdir(HANA_BASE_PATH):

            entry_path = path.join(HANA_BASE_PATH, entry)
            sapcontrol_path = path.join(entry_path, HANA_SAPCONTROL_PATH)
            entry_manifest_path = path.join(entry_path, HANA_MANIFEST_PATH)
            if path.isfile(entry_manifest_path):
                for instance in listdir(entry_path):
                    instance_number = None
                    if 'HDB' in instance:
                        instance_number = instance[-2:]
                    if not instance_number:
                        continue
                    installed = True
                    admin_name = '{}adm'.format(entry.lower())
                    running = get_instance_status(instance_number, sapcontrol_path, admin_name)
                    any_running = any_running or running
                    result.append(
                        SapHanaInstanceInfo(
                            name=entry,
                            manifest=parse_manifest(entry_manifest_path),
                            path=entry_path,
                            instance_number=instance_number,
                            running=running,
                            admin=admin_name
                        )
                    )
    return SapHanaInfo(instances=result, running=any_running, installed=installed)


def get_instance_status(instance_number, sapcontrol_path, admin_name):
    """ Gets the status for the instance given """
    try:
        output = run([
            'sudo', '-u', admin_name, sapcontrol_path, '-nr', instance_number, '-function', 'GetProcessList'])
        if output['exit_code'] == 3:
            # GetProcessList succeeded, all processes running correctly
            return True
        if output['exit_code'] == 4:
            # GetProcessList succeeded, all processes stopped
            return False
        # SAP Hana might be somewhere in between (Starting/Stopping)
        return len(output['stdout'].split('\n')) > 7
    except CalledProcessError:
        api.current_logger().warn(
            'Failed to retrieve SAP HANA instance status from sapcontrol - Considering it not running')
        return False
