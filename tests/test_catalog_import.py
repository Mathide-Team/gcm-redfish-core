"""Valide l'import du catalogue Dell et son branchement sur le moteur de dépendances.

Exécution : python test_catalog_import.py
"""

from gcm_redfish_core import (
    ComponentState,
    DependencyEngine,
    FirmwareVersion,
    KnowledgeBase,
    Repository,
    ServerProfile,
)
from gcm_redfish_core.catalog_import import (
    DellCatalogImporter,
    HPESmartComponentImporter,
    classify_dell_component,
    classify_lenovo_component,
)


def test_classification() -> None:
    assert classify_dell_component("BIOS", "Dell PowerEdge System BIOS") == "bios"
    assert classify_dell_component("FRMW", "Dell iDRAC9 Firmware,2.72") == "bmc"
    assert classify_dell_component("FRMW", "Dell PERC H730P RAID Controller Firmware") == "raid"
    assert classify_dell_component("FRMW", "Broadcom NIC Firmware") == "nic"
    print("OK : classification best-effort correcte sur les cas connus.")


def test_lenovo_classification() -> None:
    assert classify_lenovo_component("IMM2", "Integrated Management Module firmware") == "bmc"
    assert classify_lenovo_component("XCC", "XClarity Controller firmware") == "bmc"
    assert classify_lenovo_component("UEFI", "ThinkSystem UEFI") == "bios"
    assert classify_lenovo_component("RAID", "ServeRAID M5210 firmware") == "raid"
    assert classify_lenovo_component("NIC.Slot.1", "Broadcom Ethernet Adapter") == "nic"
    assert classify_lenovo_component("XYZ", "composant totalement inconnu") == "unknown"
    print("OK : classification Lenovo/IBM (IMM/XCC) correcte sur les cas connus.\n")


def test_import_and_plan() -> None:
    repo = Repository()
    importer = DellCatalogImporter()
    count = importer.populate(repo, "gcm_redfish_core/data/sample_dell_catalog.xml")
    print(f"OK : {count} paquets importés depuis le catalogue Dell d'exemple.")

    bios_versions = [str(p.version) for p in repo.available_versions("bios")]
    assert bios_versions == ["1.45", "2.00", "2.36"], bios_versions
    assert repo.find("bmc", FirmwareVersion("2.72")) is not None
    assert repo.find("raid", FirmwareVersion("7.10")) is not None

    # On réutilise la Knowledge Base déjà validée (scénario 1) : le
    # Repository, lui, vient désormais d'un vrai catalogue Dell importé.
    kb = KnowledgeBase.from_json_file("gcm_redfish_core/data/sample_kb.json")
    profile = ServerProfile(
        model="DL380 Gen10",
        vendor="hpe",  # la KB d'exemple est indexée sous "hpe" (cf. scénario existant)
        components={"bios": ComponentState("bios", FirmwareVersion("1.20"))},
    )
    plan = DependencyEngine(kb, repo).plan_update(profile, {"bios": FirmwareVersion("2.36")})
    print(plan.summary())
    assert [str(s.to_version) for s in plan.steps] == ["1.45", "2.00", "2.36"]
    assert all(s.package_id != "<paquet manquant>" for s in plan.steps)
    print("OK : le plan utilise bien les package_id réels issus du catalogue importé.\n")


def test_hpe_import() -> None:
    repo = Repository()
    importer = HPESmartComponentImporter()
    count = importer.populate(repo, "gcm_redfish_core/data/hpe_components")
    print(f"OK : {count} composants HPE importés depuis les métadonnées JSON d'exemple.")

    ilo_pkg = repo.find("bmc", FirmwareVersion("2.72"))
    assert ilo_pkg is not None, "iLO 2.72 aurait dû être classé en 'bmc'"
    assert ilo_pkg.updatable_by == ("Bmc",)
    assert ilo_pkg.reboot_required is False  # flashable par le BMC seul, sans UEFI

    bios_pkg = repo.find("bios", FirmwareVersion("2.36"))
    assert bios_pkg is not None, "le System ROM aurait dû être classé en 'bios'"
    assert "Uefi" in bios_pkg.updatable_by
    assert bios_pkg.reboot_required is True
    print("OK : classification, version et UpdatableBy corrects pour les 2 composants HPE.\n")


if __name__ == "__main__":
    test_classification()
    test_lenovo_classification()
    test_import_and_plan()
    test_hpe_import()
    print("Tous les tests d'import ont passé les assertions.")
