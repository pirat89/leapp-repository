import subprocess
from io import BytesIO

from leapp.compat import IS_PYTHON3, unicode_type
from leapp.libraries.actor import scansaphana

SAPHANA_TEST_INSTANCES = ('00', '10', '23', '34', '66')
SAPHANA2_MANIFEST = ''.join(('''compversion-id: 73554900100200005327
comptype: HDB
keyname: HDB
keycaption: SAP HANA DATABASE
supported-phases: prepare,offline,configure,online
requires-restart: system
keyvendor: sap.com
release: 2.00
rev-number: 053
rev-patchlevel: 00
rev-changelist: 1605092543
max_sps12_rev-number: 122
max_sps12_patchlevel: 33
max_rel2.0_sps0_rev-number: 02
max_rel2.0_sps0_patchlevel: 02
max_rel2.0_sps1_rev-number: 12
max_rel2.0_sps1_patchlevel: 05
max_rel2.0_sps2_rev-number: 24
max_rel2.0_sps2_patchlevel: 10
max_rel2.0_sps3_rev-number: 37
max_rel2.0_sps3_patchlevel: 07
max_rel2.0_sps4_rev-number: 48
max_rel2.0_sps4_patchlevel: 02
upgrade-restriction: sourceVersion="[1.00.000.00,1.00.69.07)"; message="You must first upgrade your system to the ''',
                             '''latest HANA 1.0 SPS6 Revision (69.07), then to HANA 1.0 SPS12 if you want to ''',
                             '''upgrade to HANA 2.0"
upgrade-restriction: sourceVersion="[1.00.69.07,1.00.120.00)"; message="You must first make an intermediate update ''',
                             '''to HANA SPS12 if you want to upgrade to HANA 2.0. Please see SAP Note 2372809."
restrict-supported-components: name="REMOTE_DATA_SYNC"; vendor="sap.com"; version="[2.0.010.00,3.00.000.00)"
sp-number: 053
sp-patchlevel: 00
makeid: 7694334
date: 2020-11-11 12:12:22
platform: linuxx86_64
hdb-state: RAMP
fullversion: 2.00.053.00 Build 1605092543-1530
auxversion: 0000.00.0
cloud_edition: 0000.00.00
changeinfo: CONT 49fd3e766fbee9a5c22dd609397d2fa640b9df09 (fa/hana2sp05)
compiletype: rel
compilebranch: fa/hana2sp05
git-hash: 49fd3e766fbee9a5c22dd609397d2fa640b9df09
git-headcount: 500015
git-mergetime: 2020-11-11 12:02:23
git-mergeepoch: 1605092543
sapexe-version: 753
sapexe-branch: 753_REL
sapexe-changelist: 2007209
compiler-version-full: gcc (SAP release 20200227, based on SUSE gcc9-9.2.1+r275327-1.3.7) 9.2.1 20190903 ''',
                             '''[gcc-9-branch revision 275330]
compiler-version: GCC 9
lcmserver-artifact-version: 2.5.46'''))


class CallMock(object):
    def __init__(self, ret):
        self.args = None
        self.ret = ret

    def __call__(self, *args):
        self.args = args
        return self.ret


class SubprocessCall(object):
    def __init__(self, admusername):
        self.admusername = admusername

    def __call__(self, *args, **kwargs):
        assert args[0][0:3] == ['sudo', '-u', self.admusername]
        cmd = args[0][3:]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        p.wait()
        return {'exit_code': p.returncode, 'stdout': p.stdout.read()}


def test_scansaphana_get_instance_status(monkeypatch):
    call = CallMock(ret={})
    monkeypatch.setattr(scansaphana, 'run', call)

    call.ret = {'exit_code': 3, 'stdout': ''}
    assert scansaphana.get_instance_status('00', 'fake/control/path', 'tstadm')
    assert call.args[0] == ['sudo', '-u', 'tstadm', 'fake/control/path', '-nr', '00', '-function', 'GetProcessList']

    call.ret = {'exit_code': 4, 'stdout': ''}
    assert not scansaphana.get_instance_status('00', 'fake/control/path', 'tstadm')
    assert call.args[0] == ['sudo', '-u', 'tstadm', 'fake/control/path', '-nr', '00', '-function', 'GetProcessList']

    call.ret = {'exit_code': 0, 'stdout': '\n'}
    assert not scansaphana.get_instance_status('00', 'fake/control/path', 'tstadm')
    assert call.args[0] == ['sudo', '-u', 'tstadm', 'fake/control/path', '-nr', '00', '-function', 'GetProcessList']

    call.ret = {'exit_code': 0, 'stdout': ' \n' * 6}
    assert not scansaphana.get_instance_status('00', 'fake/control/path', 'tstadm')
    assert call.args[0] == ['sudo', '-u', 'tstadm', 'fake/control/path', '-nr', '00', '-function', 'GetProcessList']

    call.ret = {'exit_code': 0, 'stdout': '\n' * 7}
    assert scansaphana.get_instance_status('00', 'fake/control/path', 'tstadm')
    assert call.args[0] == ['sudo', '-u', 'tstadm', 'fake/control/path', '-nr', '00', '-function', 'GetProcessList']


def test_scansaphana_parse_manifest(monkeypatch):
    class _mock_open(object):
        def __init__(self, path, mode):
            self._fp = BytesIO(SAPHANA2_MANIFEST.encode('utf-8'))

        def __enter__(self):
            return self._fp

        def __exit__(self, *args, **kwargs):
            return None

    monkeypatch.setattr(scansaphana, 'open', _mock_open, False)
    data = scansaphana.parse_manifest('yadda')
    assert data['sapexe-version'] == '753'
    assert data['keycaption'] == 'SAP HANA DATABASE'


def test_scansaphana_search_saphana_databases(monkeypatch, tmpdir):
    base_path = tmpdir
    monkeypatch.setattr(scansaphana, 'HANA_BASE_PATH', str(base_path))
    cur_path = base_path
    manifest_path_parts = scansaphana.HANA_MANIFEST_PATH.split('/')
    for part in manifest_path_parts[0:-1]:
        cur_path = cur_path.join(part)
        cur_path.mkdir()

    linkpath = base_path.join(manifest_path_parts[0])

    manifest = cur_path.join(manifest_path_parts[-1])
    manifest.write_text(unicode_type(SAPHANA2_MANIFEST), encoding='utf-8')

    sapcontrol = cur_path.join(scansaphana.HANA_SAPCONTROL_PATH.split('/')[-1])
    sapcontrol.write_text(unicode_type('''#!/bin/bash
echo 'Line1'
echo 'Line2'
echo 'Line3'
echo 'Line4'
echo 'Line5'
echo 'Line6'
echo 'Line7'
echo 'Line8'
exit 3
    '''), encoding='utf-8')
    sapcontrol.chmod(0o755)

    result = scansaphana.search_sap_hana_instances()
    assert not result.instances
    assert not result.running
    assert not result.installed

    monkeypatch.setattr(scansaphana, 'run', SubprocessCall('lppadm'))
    admin = base_path.join('LPP')
    admin.mkdir()
    admin.join(manifest_path_parts[0]).mksymlinkto(str(linkpath))

    for instance in SAPHANA_TEST_INSTANCES:
        instance_path = admin.join(('HDB' + instance))
        instance_path.mkdir()

    result = scansaphana.search_sap_hana_instances()
    assert result.instances
    assert len(result.instances) == len(SAPHANA_TEST_INSTANCES)
    check_instance_numbers = set(SAPHANA_TEST_INSTANCES)
    for instance in result.instances:
        assert instance.instance_number in check_instance_numbers
        check_instance_numbers.remove(instance.instance_number)
    assert not check_instance_numbers
    assert result.running
    assert result.installed
