from leapp.actors import Actor
from leapp.libraries.actor.checksaphana import perform_check
from leapp.models import SapHanaInfo
from leapp.reporting import Report
from leapp.tags import FactsPhaseTag, IPUWorkflowTag


class CheckSapHana(Actor):
    """
    No documentation has been provided for the check_sap_hana actor.
    """

    name = 'check_sap_hana'
    consumes = (SapHanaInfo,)
    produces = (Report,)
    tags = (IPUWorkflowTag, FactsPhaseTag)

    def process(self):
        perform_check()
