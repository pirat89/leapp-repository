
from leapp import reporting
from leapp.actors import Actor
from leapp.libraries.common.config import architecture
from leapp.reporting import Report, create_report
from leapp.tags import ChecksPhaseTag, IPUWorkflowTag


class CheckSystemArch(Actor):
    """
    Check if system is running at a supported archtecture. If no, inhibit the upgrade process.

    Base on collected system facts, verify if current archtecture is supported, otherwise produces
    a message to inhibit upgrade process
    """

    name = 'check_system_arch'
    consumes = ()
    produces = (Report,)
    tags = (ChecksPhaseTag, IPUWorkflowTag)

    def process(self):
        supported_arches = [
            architecture.ARCH_X86_64,
            architecture.ARCH_PPC64LE,
            architecture.ARCH_ARM64,
            architecture.ARCH_S390X,
            ]
        if not architecture.matches_architecture(supported_arches):
            create_report([
                reporting.Title('Unsupported architecture'),
                reporting.Summary('Upgrade process is not supported for this architecture.'),
                reporting.Severity(reporting.Severity.HIGH),
                reporting.Tags([reporting.Tags.SANITY]),
                reporting.Flags([reporting.Flags.INHIBITOR])
            ])
