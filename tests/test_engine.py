"""Démonstration du Firmware Dependency Engine sur trois scénarios.

Exécution : python test_engine.py
"""

from gcm_redfish_core import (
    ComponentState,
    DependencyEngine,
    FirmwarePackage,
    FirmwareVersion,
    KnowledgeBase,
    Repository,
    ServerProfile,
)


def build_repository() -> Repository:
    repo = Repository()
    packages = [
        ("bios", "1.45", 15),
        ("bios", "2.00", 15),
        ("bios", "2.36", 15),
        ("bios", "2.30", 15),
        ("bios", "2.78", 15),
        ("bmc", "2.72", 8),
        ("raid", "7.10", 12),
    ]
    for component_type, version, minutes in packages:
        repo.add(
            FirmwarePackage(
                component_type=component_type,
                version=FirmwareVersion(version),
                package_id=f"{component_type}-{version}.fwpkg",
                reboot_required=True,
                estimated_minutes=minutes,
            )
        )
    return repo


def scenario_1_chemin_avec_palier_et_prerequis(kb: KnowledgeBase, repo: Repository) -> None:
    print("=== Scénario 1 : BIOS 1.20 -> 2.36 (paliers obligatoires) ===")
    profile = ServerProfile(
        model="DL380 Gen10",
        vendor="hpe",
        components={"bios": ComponentState("bios", FirmwareVersion("1.20"))},
    )
    engine = DependencyEngine(kb, repo)
    plan = engine.plan_update(profile, {"bios": FirmwareVersion("2.36")})
    print(plan.summary())
    assert [str(s.to_version) for s in plan.steps] == ["1.45", "2.00", "2.36"]
    assert plan.is_safe_to_execute
    print("OK : 3 paliers dans le bon ordre, aucun problème détecté.\n")


def scenario_2_prerequis_croise(kb: KnowledgeBase, repo: Repository) -> None:
    print("=== Scénario 2 : BIOS 2.10 -> 2.78 avec prérequis BMC ===")
    profile = ServerProfile(
        model="DL380 Gen10",
        vendor="hpe",
        components={
            "bios": ComponentState("bios", FirmwareVersion("2.10")),
            "bmc": ComponentState("bmc", FirmwareVersion("2.55")),
            "raid": ComponentState("raid", FirmwareVersion("6.62")),
        },
    )
    engine = DependencyEngine(kb, repo)
    plan = engine.plan_update(
        profile,
        {
            "bios": FirmwareVersion("2.78"),
            "bmc": FirmwareVersion("2.72"),
            "raid": FirmwareVersion("7.10"),
        },
    )
    print(plan.summary())
    # Le BMC doit être mis à jour AVANT le passage du BIOS à 2.30.
    bmc_index = next(i for i, s in enumerate(plan.steps) if s.component_type == "bmc")
    bios_230_index = next(
        i for i, s in enumerate(plan.steps) if s.component_type == "bios" and str(s.to_version) == "2.30"
    )
    assert bmc_index < bios_230_index, "Le prérequis croisé BMC avant BIOS 2.30 n'est pas respecté"
    assert plan.is_safe_to_execute
    print("OK : le BMC est bien planifié avant le palier BIOS 2.30.\n")


def scenario_3_incompatibilite_bloquante(kb: KnowledgeBase, repo: Repository) -> None:
    print("=== Scénario 3 : BIOS 2.30 visé alors que RAID reste sous 6.0 (plage incompatible) ===")
    for raid_version in ["5.90", "5.99", "5.10"]:
        profile = ServerProfile(
            model="DL380 Gen10",
            vendor="hpe",
            components={
                "bios": ComponentState("bios", FirmwareVersion("2.10")),
                "bmc": ComponentState("bmc", FirmwareVersion("2.72")),
                "raid": ComponentState("raid", FirmwareVersion(raid_version)),
            },
        )
        engine = DependencyEngine(kb, repo)
        plan = engine.plan_update(profile, {"bios": FirmwareVersion("2.30")})
        assert plan.has_blocking_issues, f"RAID {raid_version} aurait dû être bloqué"
        assert not plan.is_safe_to_execute
    print(f"OK : la plage RAID < 6.0 bloque bien {['5.90', '5.99', '5.10']}, "
          "pas seulement une version exacte.")

    # Contre-exemple : RAID déjà à 7.10 (hors plage incompatible) ne doit pas bloquer.
    profile_ok = ServerProfile(
        model="DL380 Gen10",
        vendor="hpe",
        components={
            "bios": ComponentState("bios", FirmwareVersion("2.10")),
            "bmc": ComponentState("bmc", FirmwareVersion("2.72")),
            "raid": ComponentState("raid", FirmwareVersion("7.10")),
        },
    )
    plan_ok = DependencyEngine(kb, repo).plan_update(profile_ok, {"bios": FirmwareVersion("2.30")})
    assert not plan_ok.has_blocking_issues
    print("OK : RAID 7.10 (hors plage) n'est pas bloqué.\n")


def scenario_4_chemin_inconnu(repo: Repository) -> None:
    print("=== Scénario 4 : composant sans aucune règle connue ===")
    empty_kb = KnowledgeBase()
    profile = ServerProfile(
        model="ThinkSystem SR650",
        vendor="lenovo",
        components={"nic": ComponentState("nic", FirmwareVersion("4.10"))},
    )
    engine = DependencyEngine(empty_kb, repo)
    plan = engine.plan_update(profile, {"nic": FirmwareVersion("4.34")})
    print(plan.summary())
    assert plan.requires_manual_validation
    assert not plan.is_safe_to_execute
    print("OK : absence de règles => validation manuelle exigée, aucune étape auto-générée.\n")


if __name__ == "__main__":
    kb = KnowledgeBase.from_json_file("gcm_redfish_core/data/sample_kb.json")
    repo = build_repository()
    scenario_1_chemin_avec_palier_et_prerequis(kb, repo)
    scenario_2_prerequis_croise(kb, repo)
    scenario_3_incompatibilite_bloquante(kb, repo)
    scenario_4_chemin_inconnu(repo)
    print("Tous les scénarios ont passé les assertions.")
