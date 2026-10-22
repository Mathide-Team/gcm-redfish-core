"""Cas limites du moteur de dépendances, des versions et des importeurs (issue #4).

Complète ``test_engine.py`` et ``test_catalog_import.py`` : règles absentes,
versions hors graphe, paquets manquants, prérequis inatteignables, cycles,
importeur HPE (aucun test jusqu'ici) et entrées incomplètes des catalogues.

Exécution : python3 tests/test_moteur_et_import_limites.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from gcm_redfish_core import (
    ComponentState,
    DellCatalogImporter,
    DependencyEngine,
    FirmwarePackage,
    FirmwareVersion,
    HPESmartComponentImporter,
    KnowledgeBase,
    PrerequisiteRule,
    Repository,
    ServerProfile,
    Severity,
)
from gcm_redfish_core.catalog_import import (
    classify_dell_component,
    classify_hpe_component,
    is_confident_dell_classification,
)
from gcm_redfish_core.knowledge_base import VersionConstraint

V = FirmwareVersion


def _paquet(composant: str, version: str) -> FirmwarePackage:
    return FirmwarePackage(
        component_type=composant,
        version=V(version),
        package_id=f"{composant}-{version}.bin",
        reboot_required=False,
        estimated_minutes=5,
    )


def _profil(**versions: str) -> ServerProfile:
    return ServerProfile(
        model="M1",
        vendor="hpe",
        components={c: ComponentState(c, V(v)) for c, v in versions.items()},
    )


def _severites(plan) -> list[Severity]:
    return [probleme.severity for probleme in plan.issues]


def scenario_versions_firmware() -> None:
    assert V("2.36rc1") < V("2.36.1"), "pré-version après la suivante"
    assert V("2.36rc1") < V("2.36"), "pré-version après la finale"
    assert V("1.0").__eq__("1.0") is NotImplemented
    assert V("1.0").__lt__("1.0") is NotImplemented
    assert len({V("1.2"), V("1.2"), V("1.3")}) == 2
    assert repr(V(" 2.0 ")) == "FirmwareVersion('2.0')"
    print("OK : versions mixtes, comparaisons avec un autre type, hash, repr.\n")


def scenario_contraintes_de_version() -> None:
    try:
        VersionConstraint("=>", V("1.0"))
    except ValueError:
        pass
    else:
        raise AssertionError("opérateur inconnu accepté")
    attendu = {"==": False, "!=": True, "<": True, "<=": True, ">": False, ">=": False}
    for operateur, resultat in attendu.items():
        assert VersionConstraint(operateur, V("2.0")).matches(V("1.0")) is resultat, operateur
    assert VersionConstraint(">=", V("1.0")).matches(None) is False
    print("OK : opérateur inconnu refusé, six opérateurs, version absente.\n")


def scenario_depot_paquet_introuvable() -> None:
    depot = Repository([_paquet("bios", "1.0")])
    assert depot.find("bios", V("9.9")) is None
    assert depot.find("bios", V("1.0")).package_id == "bios-1.0.bin"
    print("OK : Repository.find() sans correspondance.\n")


def scenario_moteur_cas_limites() -> None:
    kb = KnowledgeBase()
    kb.add_upgrade_edges("hpe", "M1", "bios", {"1.0": ["2.0"], "2.0": ["3.0"], "4.0": []})
    depot = Repository([_paquet("bios", "2.0")])  # palier 3.0 sans paquet
    moteur = DependencyEngine(kb, depot)

    plan = moteur.plan_update(_profil(bios="1.0"), {"bios": V("1.0")})
    assert plan.steps == [] and plan.issues == [], "aucun changement attendu"

    plan = moteur.plan_update(_profil(bmc="1.0"), {"bmc": V("2.0")})
    assert _severites(plan) == [Severity.MANUAL_REVIEW], "aucune règle connue"

    plan = moteur.plan_update(_profil(bios="0.5"), {"bios": V("2.0")})
    assert _severites(plan) == [Severity.MANUAL_REVIEW], "version hors graphe"

    plan = moteur.plan_update(_profil(bios="1.0"), {"bios": V("4.0")})
    assert _severites(plan) == [Severity.BLOCKING], "aucun chemin"

    plan = moteur.plan_update(_profil(bios="1.0"), {"bios": V("3.0")})
    assert [s.package_id for s in plan.steps] == ["bios-2.0.bin", "<paquet manquant>"], plan.steps
    assert Severity.MANUAL_REVIEW in _severites(plan)

    plan = moteur.plan_update(_profil(), {"bios": V("2.0")})
    assert _severites(plan) == [Severity.MANUAL_REVIEW], "version courante inconnue"

    chaines = {"bios": plan.steps}
    assert moteur._final_version(_profil(bios="1.0"), "bios", {"bios": []}) == V("1.0")
    assert moteur._final_version(_profil(), "bios", chaines) is None
    print("OK : sans changement, sans règle, hors graphe, sans chemin, paquet manquant, version inconnue.\n")


def scenario_prerequis_et_cycle() -> None:
    kb = KnowledgeBase()
    kb.add_upgrade_edges("hpe", "M1", "bios", {"1.0": ["2.0"]})
    kb.add_upgrade_edges("hpe", "M1", "bmc", {"1.0": ["2.0"]})
    depot = Repository([_paquet("bios", "2.0"), _paquet("bmc", "2.0")])

    def regle(cible: str, requis: str, minimum: str) -> PrerequisiteRule:
        return PrerequisiteRule("hpe", "M1", cible, V("2.0"), requis, V(minimum))

    kb.add_prerequisite(regle("bios", "bmc", "1.5"))
    kb.add_prerequisite(regle("bios", "nic", "3.0"))  # nic absent du plan : inatteignable
    kb.add_prerequisite(PrerequisiteRule("hpe", "M1", "bios", V("9.9"), "bmc", V("1.0")))  # palier hors chemin
    plan = DependencyEngine(kb, depot).plan_update(_profil(bios="1.0", bmc="1.0"), {"bios": V("2.0"), "bmc": V("2.0")})
    assert [s.component_type for s in plan.steps] == ["bmc", "bios"], plan.steps
    assert _severites(plan) == [Severity.BLOCKING], plan.issues

    deja = DependencyEngine(kb, depot).plan_update(_profil(bios="1.0", bmc="1.6", nic="3.0"), {"bios": V("2.0")})
    assert _severites(deja) == [], deja.issues

    kb.add_prerequisite(regle("bmc", "bios", "2.0"))  # bios attend bmc, bmc attend bios
    cycle = DependencyEngine(kb, depot).plan_update(
        _profil(bios="1.0", bmc="1.0", nic="3.0"), {"bios": V("2.0"), "bmc": V("2.0")}
    )
    assert Severity.BLOCKING in _severites(cycle) and cycle.steps == [], cycle
    print("OK : prérequis ordonné, inatteignable, déjà satisfait, hors chemin, cycle détecté.\n")


def scenario_import_hpe() -> None:
    with tempfile.TemporaryDirectory() as dossier:
        base = Path(dossier)
        (base / "a.json").write_text(
            json.dumps({"Filename": "U46_2.68.fwpkg", "Name": "System ROM U46", "UpdatableBy": ["Uefi"]}),
            encoding="utf-8",
        )
        (base / "b.json").write_text(
            json.dumps({"Filename": "ilo6_168.fwpkg", "Name": "iLO 6", "UpdatableBy": ["Bmc"]}), encoding="utf-8"
        )
        (base / "c.json").write_text(json.dumps({"Name": "sans_version.fwpkg"}), encoding="utf-8")
        (base / "d.json").write_text(json.dumps({"UpdatableBy": []}), encoding="utf-8")
        (base / "e.json").write_text(json.dumps({"Filename": "cpld-1.0.2.fwpkg"}), encoding="utf-8")
        paquets = HPESmartComponentImporter().parse(base)
        un_seul = HPESmartComponentImporter().parse(base / "e.json")
    assert [(p.component_type, str(p.version), p.reboot_required) for p in paquets] == [
        ("bios", "2.68", True),
        ("cpld", "1.0.2", True),
    ], paquets
    assert len(un_seul) == 1
    assert classify_hpe_component("Carte inconnue") == "unknown"
    assert classify_hpe_component("") == "unknown"
    print("OK : importeur HPE (dossier et fichier), entrées sans nom ou sans version ignorées.\n")


def scenario_import_dell_entrees_incompletes() -> None:
    xml = """<?xml version="1.0"?>
<Manifest xmlns="urn:dell">
  <SoftwareComponent packageID="P1" dellVersion="2.1.0">
    <Name><Display lang="en">PERC H755</Display></Name>
  </SoftwareComponent>
  <SoftwareComponent packageID="P2">
    <ComponentType value="BIOS"/>
  </SoftwareComponent>
  <SoftwareComponent releaseID="P3" vendorVersion="1.0">
    <ComponentType value="APAC"/>
    <Name><Display lang="en"></Display></Name>
  </SoftwareComponent>
</Manifest>"""
    with tempfile.TemporaryDirectory() as dossier:
        catalogue = Path(dossier) / "Catalog.xml"
        catalogue.write_text(xml, encoding="utf-8")
        paquets = DellCatalogImporter().parse(catalogue)
    assert [(p.package_id, p.component_type) for p in paquets] == [("P1", "raid"), ("P3", "apac")], paquets
    assert classify_dell_component("", "") == "unknown"
    assert is_confident_dell_classification("raid") and not is_confident_dell_classification("apac")
    print("OK : catalogue Dell à namespace, entrées incomplètes ignorées, replis de classification.\n")


if __name__ == "__main__":
    scenario_versions_firmware()
    scenario_contraintes_de_version()
    scenario_depot_paquet_introuvable()
    scenario_moteur_cas_limites()
    scenario_prerequis_et_cycle()
    scenario_import_hpe()
    scenario_import_dell_entrees_incompletes()
    print("Tous les cas limites ont passé les assertions.")
