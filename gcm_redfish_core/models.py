"""Modèles de données du moteur de dépendances firmware.

Ce module ne dépend ni de GTK ni de Redfish directement : il décrit l'état
d'un serveur et les paquets firmware disponibles, sous une forme que
n'importe quelle source (Redfish, catalogue constructeur, import manuel)
peut alimenter.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .firmware_version import FirmwareVersion


@dataclass(frozen=True)
class FirmwarePackage:
    """Un paquet firmware disponible dans un dépôt.

    Attributes:
        component_type: Type de composant (ex. ``"bios"``, ``"bmc"``,
            ``"raid"``, ``"nic"``). Le référentiel de règles utilise ces
            mêmes identifiants.
        version: Version fournie par ce paquet.
        package_id: Identifiant/chemin du paquet dans le dépôt.
        reboot_required: Si l'installation nécessite un redémarrage.
        estimated_minutes: Durée estimée de l'installation (+ redémarrage
            éventuel).
        updatable_by: Agents capables de flasher ce paquet (ex. ``"Bmc"``,
            ``"RuntimeAgent"``, ``"Uefi"`` — vocabulaire HPE ``UpdatableBy``
            confirmé dans les métadonnées ``payload.json`` des Smart
            Components). Vide si inconnu/non applicable.
    """

    component_type: str
    version: FirmwareVersion
    package_id: str
    reboot_required: bool = True
    estimated_minutes: int = 10
    updatable_by: tuple[str, ...] = ()


@dataclass
class ComponentState:
    """État actuel d'un composant sur un serveur donné."""

    component_type: str
    current_version: FirmwareVersion


@dataclass
class ServerProfile:
    """Identité et état firmware d'un serveur, tels que remontés par Redfish.

    Attributes:
        model: Modèle exact (ex. ``"DL380 Gen10"``), utilisé pour
            sélectionner les règles de dépendances applicables.
        vendor: Constructeur (ex. ``"hpe"``, ``"dell"``, ``"lenovo"``).
        components: État courant de chaque composant suivi.
    """

    model: str
    vendor: str
    components: dict[str, ComponentState] = field(default_factory=dict)

    def current_version(self, component_type: str) -> FirmwareVersion | None:
        state = self.components.get(component_type)
        return state.current_version if state else None


class Repository:
    """Dépôt de paquets firmware disponibles (local, HTTP, etc.).

    Le moteur de dépendances ne se soucie pas d'où viennent les paquets ;
    il consulte seulement ce qui est *disponible* pour construire un
    chemin de mise à jour exécutable.
    """

    def __init__(self, packages: list[FirmwarePackage] | None = None) -> None:
        self._packages: list[FirmwarePackage] = list(packages or [])

    def add(self, package: FirmwarePackage) -> None:
        self._packages.append(package)

    def available_versions(self, component_type: str) -> list[FirmwarePackage]:
        """Paquets disponibles pour un composant, triés par version croissante."""
        matches = [p for p in self._packages if p.component_type == component_type]
        return sorted(matches, key=lambda p: p.version)

    def find(self, component_type: str, version: FirmwareVersion) -> FirmwarePackage | None:
        for pkg in self._packages:
            if pkg.component_type == component_type and pkg.version == version:
                return pkg
        return None
