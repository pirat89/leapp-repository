from leapp.actors import Actor
from leapp.tags import ChecksPhaseTag, IPUWorkflowTag
from leapp.models import InstalledRedHatSignedRPM
from leapp.models import Report
from leapp import reporting
from leapp.reporting import create_report
from leapp.libraries.common.rpms import has_package


class CheckWireshark(Actor):
    """
    Report a couple of changes in tshark usage
    """

    name = 'check_wireshark'
    consumes = (InstalledRedHatSignedRPM, )
    produces = (Report, )
    tags = (ChecksPhaseTag, IPUWorkflowTag)

    def process(self):
        if has_package(InstalledRedHatSignedRPM, 'wireshark'):
            create_report([
                reporting.Title('tshark: changed cli option'),
                reporting.Summary(
                    'The -C suboption for -N option for asynchronous DNS name resolution '
                    'has been completely removed from tshark. The reason for this is that '
                    'the asynchronous DNS resolution is now the only resolution available '
                    'so there is no need for -C. If you are using -NC with tshark in any '
                    'of your scripts, please remove it.'),
                reporting.Severity(reporting.Severity.LOW),
                reporting.Tags([reporting.Tags.SANITY, reporting.Tags.MONITORING]),
            ])

            create_report([
                reporting.Title('tshark: changed cli output'),
                reporting.Summary(
                    'When using -H option with capinfos, the output no longer shows MD5 hashes. '
                    'Now it shows SHA256 instead. SHA1 might get removed very soon as well. '
                    'If you use these output values, please change your scripts.'),
                reporting.Severity(reporting.Severity.LOW),
                reporting.Tags([reporting.Tags.SANITY, reporting.Tags.MONITORING]),
            ])
