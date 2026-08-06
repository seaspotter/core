from dataclasses import dataclass


@dataclass
class SaicIsmartConfiguration:
    saic_user: str = ""
    saic_password: str = ""
    vin: str = ""
    # SAIC-Region: eu (Standard), au, tr - siehe SAIC-iSmart-API/documentation
    region: str = "eu"
    calculate_soc: bool = False


@dataclass
class SaicIsmartSetup:
    name: str = "MG iSMART (SAIC)"
    type: str = "saic_ismart"
    official: bool = False
    configuration: SaicIsmartConfiguration = None

    def __post_init__(self):
        if self.configuration is None:
            self.configuration = SaicIsmartConfiguration()
