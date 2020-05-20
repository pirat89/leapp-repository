from leapp.models import Model, fields
from leapp.topics import SystemInfoTopic


class SapHanaInstanceInfo(Model):
    """ Details of SAP HANA instances """
    topic = SystemInfoTopic

    name = fields.String()
    """ Name of the instance """
    path = fields.String()
    """ Directory of the SAP HANA instance """
    instance_number = fields.String()
    """ SAP HANA Instance number """
    running = fields.Boolean()
    """ Is the instance is currently running? """
    admin = fields.String()
    """ Name of the instance administrator """
    manifest = fields.JSON()
    """ Content of the SAP HANA manifest file """


class SapHanaInfo(Model):
    """
    This message contains information collected around SAP Hana

    If SAP HANA has been detected on the machine, it is running and which versions have been detected.
    """
    topic = SystemInfoTopic
    installed = fields.Boolean(default=False)
    """ True if SAP HANA has been detected on the system """
    running = fields.Boolean(default=False)
    """ True if an instance of SAP HANA is running """
    instances = fields.List(fields.Model(SapHanaInstanceInfo))
    """ List of instance details of SAP HANA detected on the system """
