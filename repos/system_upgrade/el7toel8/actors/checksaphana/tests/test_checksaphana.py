from leapp.libraries.actor import checksaphana
from leapp.libraries.stdlib import run
from leapp.libraries.common import testutils

SAPHANA1_MANIFEST = '''comptype: HDB
keyname: HDB
keycaption: SAP HANA DATABASE
supported-phases: prepare,offline,online
requires-restart: system
keyvendor: sap.com
release: 1.00
rev-number: 122
rev-patchlevel: 33
sp-number: 122
sp-patchlevel: 33
makeid: 7407089
date: 2020-10-08 16:23:37
platform: linuxx86_64
hdb-state: RAMP
fullversion: 1.00.122.33 Build 1602166441-1530
'''


SAPHANA2_MANIFEST = '''comptype: HDB
keyname: HDB
keycaption: SAP HANA DATABASE
supported-phases: prepare,offline,configure,online
requires-restart: system
keyvendor: sap.com
release: 2.00
rev-number: 053
rev-patchlevel: 00
sp-number: 053
sp-patchlevel: 00
date: 2020-11-11 12:12:22
platform: linuxx86_64
hdb-state: RAMP
fullversion: 2.00.053.00 Build 1605092543-1530
'''

SAPHANA2_LOW_VERSION_MANIFEST = '''comptype: HDB
keyname: HDB
keycaption: SAP HANA DATABASE
supported-phases: prepare,offline,configure,online
requires-restart: system
keyvendor: sap.com
release: 2.00
rev-number: 040
rev-patchlevel: 00
sp-number: 040
sp-patchlevel: 00
date: 2020-11-11 12:12:22
platform: linuxx86_64
hdb-state: RAMP
fullversion: 2.00.040.00 Build 1605092543-1530
'''


def list_clear(l):
    del l[:]


def _parse_manifest_data(manifest):
    result = {}
    for line in manifest.split('\n'):
        line = line.strip()
        if not line:
            continue
        # If there is a ValueError during the split, just burn with fire in this test
        key, value = line.split(': ', 1)
        result[key] = value
    return result


class MockSapHanaInstanceInfo(object):
    def __init__(self, name, number, path, admin, manifest_data, running=True):
        self.manifest = _parse_manifest_data(manifest_data)
        self.name = name
        self.instance_number = number
        self.path = path
        self.running = running
        self.admin = admin


def _gen_instance_info(name, manifest_data, index, running=True):
    return MockSapHanaInstanceInfo(
        name=name,
        number='{:02}'.format(index),
        path='/hana/shared/{name}/HDB{number:02}'.format(name=name, number=index),
        admin='{name}adm'.format(name=name.lower()),
        manifest_data=manifest_data,
        running=running
    )


class MockSapHanaInfo(object):
    def __init__(self, v1names, v2names, v2lownames, running=None):
        self.installed = bool(v1names or v2names or v2lownames)
        self.running = running if running is not None else self.installed
        self.instances = [_gen_instance_info(name,
                                             SAPHANA1_MANIFEST,
                                             index,
                                             running=running)
                          for index, name in enumerate(v1names)]
        self.instances += [_gen_instance_info(name,
                                              SAPHANA2_MANIFEST,
                                              index + len(v1names),
                                              running=running)
                           for index, name in enumerate(v2names)]
        self.instances += [_gen_instance_info(name,
                                              SAPHANA2_LOW_VERSION_MANIFEST,
                                              index + len(v1names) + len(v2names),
                                              running=running)
                           for index, name in enumerate(v2lownames)]


def _report_collector(reports):
    def _mock_report(args):
        reports.append(args)
    return _mock_report


def _consume_mock_sap_hana_info(v1names=(), v2names=(), v2lownames=(), running=True):
    def _consume(*models):
        return iter([MockSapHanaInfo(v1names, v2names, v2lownames, running=running)])
    return _consume


def test_checksaphana_perform_check(monkeypatch):
    v1names = ('ABC', 'DEF', 'GHI')
    v2names = ('JKL', 'MNO', 'PQR', 'STU')
    v2lownames = ('VWX', 'YZA')
    reports = []
    monkeypatch.setattr(checksaphana.reporting, 'create_report', _report_collector(reports))
    monkeypatch.setattr(checksaphana.api, 'consume', _consume_mock_sap_hana_info(
        v1names=v1names, v2names=v2names, v2lownames=v2lownames, running=True))

    for arch in (testutils.architecture.ARCH_PPC64LE,
                 testutils.architecture.ARCH_ARM64,
                 testutils.architecture.ARCH_S390X):
        for flavour in ('default', 'saphana'):
            list_clear(reports)
            monkeypatch.setattr(checksaphana.api,
                                'current_actor',
                                testutils.CurrentActorMocked(arch=arch, flavour=flavour))
            checksaphana.perform_check()
            if flavour == 'saphana':
                assert reports and len(reports) == 1
                assert 'X86_64' in reports[0][0].to_dict()['title']
            else:
                assert not reports

    list_clear(reports)
    monkeypatch.setattr(checksaphana.api,
                        'current_actor',
                        testutils.CurrentActorMocked(arch=testutils.architecture.ARCH_X86_64))
    checksaphana.perform_check()
    assert not reports

    monkeypatch.setattr(checksaphana.api,
                        'current_actor',
                        testutils.CurrentActorMocked(arch=testutils.architecture.ARCH_X86_64, flavour='saphana'))
    checksaphana.perform_check()
    assert reports
    # Expected 2 reports due to v1names + v2lownames + running
    assert len(reports) == 3

    list_clear(reports)
    monkeypatch.setattr(checksaphana.api, 'consume', _consume_mock_sap_hana_info(
        v1names=v1names, v2names=v2names, v2lownames=v2lownames, running=False))
    checksaphana.perform_check()
    assert reports
    # Expected 2 reports due to v1names + v2lownames
    assert len(reports) == 2
