"""Import de catalogues constructeurs vers le ``Repository``.

Constat vérifié (documentation publique Dell + retours communautaires) :
les catalogues constructeurs listent les versions disponibles et leur
applicabilité matérielle, mais **n'exposent pas** au format structuré les
paliers de mise à jour obligatoires ni les prérequis croisés entre
composants — cette information reste documentée en texte libre (notes de
version, advisories) et doit être saisie dans la Firmware Knowledge Base
(cf. ``knowledge_base.py``), jamais déduite automatiquement d'un catalogue.

Ce module se limite donc, volontairement, à peupler un ``Repository``
(inventaire des paquets disponibles) à partir d'un catalogue réel. Le
Dependency Engine continue de refuser toute mise à jour dont le chemin
n'est pas couvert par la Knowledge Base, quel que soit le contenu du
catalogue.

Actuellement implémenté : Dell ``Catalog.xml`` (schéma confirmé via la
documentation Dell publique — élément ``SoftwareComponent`` avec attributs
``packageID``/``releaseID``, ``dellVersion``/``vendorVersion``, ``path``,
et enfant ``ComponentType value="..."``).

HPE (SPP) et Lenovo (XClarity repository) ne sont pas encore implémentés :
leurs formats de référence (pages de notes de version HTML en texte libre
pour HPE, métadonnées XML propriétaires pour Lenovo) nécessitent un
échantillon réel pour être mappés correctement plutôt qu'un mapping
inventé. ``CatalogImporter`` définit l'interface commune à respecter le
jour où ces échantillons sont disponibles.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from pathlib import Path

from .firmware_version import FirmwareVersion
from .models import FirmwarePackage, Repository

# Correspondance best-effort entre les valeurs `ComponentType` de Dell et le
# vocabulaire interne du moteur. À affiner à partir de catalogues réels :
# la valeur `ComponentType` de Dell ne distingue pas toujours finement
# iDRAC/BIOS/RAID/NIC (ex. "FRMW" est générique), d'où le repli sur une
# recherche de mots-clés dans le nom du composant.
_DELL_COMPONENT_TYPE_MAP = {
    "bios": "bios",
}
_DELL_NAME_KEYWORDS = [
    ("idrac", "bmc"),
    ("lifecycle controller", "bmc"),
    ("chassis management", "chassis"),
    ("chassis-system-management", "chassis"),
    ("perc", "raid"),
    ("raid", "raid"),
    ("boss-n1", "boss"),
    ("boss ", "boss"),
    ("express flash", "nvme"),
    ("nvme", "nvme"),
    ("mellanox", "nic"),
    ("connectx", "nic"),
    ("nic", "nic"),
    ("ethernet", "nic"),
    ("broadcom", "nic"),
    ("qlogic", "nic"),
    ("passive backplane", "backplane"),
    ("backplane", "backplane"),
    ("trusted platform module", "tpm"),
    ("tpm", "tpm"),
    ("cpld", "cpld"),
    ("power supply", "psu"),
    ("psu", "psu"),
    # Repli tardif : certains modèles (ex. châssis XR4000w) publient leur
    # BIOS avec ComponentType=FRMW au lieu de BIOS ; observé sur un vrai
    # catalogue, donc traité en dernier recours plutôt que par défaut.
    ("bios", "bios"),
]

# Catégories que le classifieur peut renvoyer avec confiance (via la table
# ComponentType ou un mot-clé de nom). Tout ce qui n'y figure pas est un
# repli sur la valeur brute de Dell (ex. "frmw", "apac", "drvr") : le
# composant existe mais son sous-type précis n'a pas pu être déterminé.
CONFIDENT_DELL_CATEGORIES = frozenset(
    {"bios", "bmc", "raid", "boss", "nvme", "nic", "backplane", "tpm", "cpld", "psu", "chassis"}
)


def is_confident_dell_classification(component_type: str) -> bool:
    """True si ``component_type`` est une catégorie reconnue avec confiance
    (par opposition à un repli sur la valeur brute Dell, ex. "frmw")."""
    return component_type in CONFIDENT_DELL_CATEGORIES


def _local_name(tag: str) -> str:
    """Retire le préfixe de namespace XML éventuel (ex. '{ns}Tag' -> 'Tag')."""
    return tag.rsplit("}", 1)[-1]


def _find_child_text(element: ET.Element, *names: str) -> str | None:
    for child in element:
        if _local_name(child.tag) in names and child.text:
            return child.text.strip()
    return None


def classify_dell_component(component_type_value: str, display_name: str) -> str:
    """Détermine le ``component_type`` interne à partir des indices Dell disponibles.

    Heuristique best-effort : à valider/étendre contre un catalogue réel.
    """
    normalized_type = (component_type_value or "").strip().lower()
    if normalized_type in _DELL_COMPONENT_TYPE_MAP:
        return _DELL_COMPONENT_TYPE_MAP[normalized_type]

    name_lower = (display_name or "").lower()
    for keyword, component_type in _DELL_NAME_KEYWORDS:
        if keyword in name_lower:
            return component_type

    # Repli : on garde la valeur brute Dell plutôt que de la perdre, quitte
    # à ce qu'elle ne corresponde à aucune règle de la Knowledge Base (le
    # moteur la traitera alors comme "aucune règle connue", ce qui est le
    # comportement sûr par défaut).
    return normalized_type or "unknown"


class CatalogImporter(ABC):
    """Interface commune à tout importeur de catalogue constructeur."""

    vendor: str

    @abstractmethod
    def parse(self, path: str | Path) -> list[FirmwarePackage]:
        """Retourne les paquets disponibles trouvés dans le fichier de catalogue."""

    def populate(self, repository: Repository, path: str | Path) -> int:
        """Ajoute les paquets trouvés au ``Repository`` fourni.

        Returns:
            Le nombre de paquets ajoutés.
        """
        packages = self.parse(path)
        for package in packages:
            repository.add(package)
        return len(packages)


class HPESmartComponentImporter(CatalogImporter):
    """Importeur pour les métadonnées Smart Component HPE (``payload.json`` /
    ``<component>.json``).

    Champs confirmés par la documentation technique HPE publique (blog
    "HPE firmware updates: Part 1 – File types and Smart Components",
    HPE Server Management Portal) : ``Name``, ``Filename``, ``UpdatableBy``
    (``Bmc``/``RuntimeAgent``/``Uefi``), ``Targets``.

    Point important : **aucun champ de version ni de prérequis structuré
    n'est documenté** dans ces métadonnées. La version doit donc être
    déduite du nom de fichier (best-effort, regex) ou fournie séparément
    (ex. à partir des notes de version du SPP) ; en cas de doute, le
    composant est ignoré plutôt que d'inventer une version incorrecte —
    une version fausse serait pire qu'une absence de donnée pour un moteur
    de sécurité de mise à jour.

    Contrairement à Dell, HPE distribue une métadonnée par composant (un
    fichier ``.json`` par Smart Component), pas un catalogue unique : cet
    importeur accepte donc un dossier contenant plusieurs fichiers
    ``.json``, un par composant.
    """

    vendor = "hpe"

    #: Version trouvée à la fin du nom de fichier avant l'extension, ex.
    #: "16_32_1010-MCX512F-ACH_Ax_Bx.pldm.fwpkg" -> "16_32_1010" ou
    #: "HPE_UBM3_1.24_F.fwpkg" -> "1.24". Best-effort : à vérifier au cas
    #: par cas, d'où le composant ignoré (pas de levée d'exception) si rien
    #: de convaincant n'est trouvé.
    _VERSION_IN_FILENAME_RE = re.compile(
        r"[_\-](\d+(?:[._]\d+){1,3})(?:[_\-][A-Za-z]{1,3})?\.(?:fwpkg|exe|rpm|zip|scexe)$"
    )

    def parse(self, path: str | Path) -> list[FirmwarePackage]:
        path = Path(path)
        json_files = [path] if path.is_file() else sorted(path.glob("*.json"))
        packages: list[FirmwarePackage] = []

        for json_file in json_files:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            filename = data.get("Filename") or data.get("Name")
            if not filename:
                continue  # métadonnée incomplète, on ignore plutôt que deviner

            version_raw = self._guess_version(filename)
            if version_raw is None:
                continue  # pas de version fiable identifiable : on n'invente pas

            updatable_by = tuple(data.get("UpdatableBy") or ())
            component_type = classify_hpe_component(data.get("Name") or filename)

            packages.append(
                FirmwarePackage(
                    component_type=component_type,
                    version=FirmwareVersion(version_raw),
                    package_id=filename,
                    reboot_required="Uefi" in updatable_by or not updatable_by,
                    estimated_minutes=15,
                    updatable_by=updatable_by,
                )
            )
        return packages

    @classmethod
    def _guess_version(cls, filename: str) -> str | None:
        match = cls._VERSION_IN_FILENAME_RE.search(filename)
        if not match:
            return None
        return match.group(1).replace("_", ".")


_HPE_NAME_KEYWORDS = [
    ("ilo", "bmc"),
    ("bios", "bios"),
    ("system rom", "bios"),
    ("smart array", "raid"),
    ("smartarray", "raid"),
    ("nvme", "nvme"),
    ("nic", "nic"),
    ("ethernet", "nic"),
    ("network adapter", "nic"),
    ("mellanox", "nic"),
    ("cpld", "cpld"),
    ("power supply", "psu"),
]


def classify_hpe_component(display_name: str) -> str:
    """Détermine le ``component_type`` interne à partir du nom du Smart Component.

    Heuristique best-effort (aucun champ ``ComponentType`` structuré n'est
    confirmé dans les métadonnées HPE) : à affiner contre des exemples réels.
    """
    name_lower = (display_name or "").lower()
    for keyword, component_type in _HPE_NAME_KEYWORDS:
        if keyword in name_lower:
            return component_type
    return "unknown"


_LENOVO_NAME_KEYWORDS = [
    # IMM (Integrated Management Module) : BMC des System x / ThinkServer,
    # prédécesseur du XCC (XClarity Controller) sur les ThinkSystem actuels.
    ("imm", "bmc"),
    ("xcc", "bmc"),
    ("xclarity controller", "bmc"),
    ("baseboard management controller", "bmc"),
    ("uefi", "bios"),
    ("bios", "bios"),
    ("serveraid", "raid"),
    ("raid", "raid"),
    ("ethernet", "nic"),
    ("broadcom", "nic"),
    ("mellanox", "nic"),
    ("connectx", "nic"),
    ("qlogic", "nic"),
    ("emulex", "nic"),
    ("nic", "nic"),
    ("backplane", "backplane"),
    ("power supply", "psu"),
    ("psu", "psu"),
    ("tpm", "tpm"),
    ("cpld", "cpld"),
]


def classify_lenovo_component(component_id: str, display_name: str) -> str:
    """Détermine le ``component_type`` interne pour un composant Lenovo/IBM.

    Heuristique best-effort (aucun échantillon réel de catalogue/export
    XClarity n'a encore été utilisé pour valider ces mots-clés, contrairement
    à Dell et HPE) : à corriger dès qu'un vrai export est disponible.
    """
    combined = f"{component_id} {display_name}".lower()
    for keyword, component_type in _LENOVO_NAME_KEYWORDS:
        if keyword in combined:
            return component_type
    return "unknown"


class DellCatalogImporter(CatalogImporter):
    """Importeur pour le ``Catalog.xml`` public de Dell (downloads.dell.com/catalog).

    Ne lit que l'inventaire de paquets (composant, version, chemin de
    téléchargement). Ne tente pas d'en déduire des règles de dépendance.
    """

    vendor = "dell"

    def parse(self, path: str | Path) -> list[FirmwarePackage]:
        tree = ET.parse(path)
        root = tree.getroot()
        packages: list[FirmwarePackage] = []

        for element in root.iter():
            if _local_name(element.tag) != "SoftwareComponent":
                continue

            package_id = element.get("packageID") or element.get("releaseID")
            version_raw = element.get("dellVersion") or element.get("vendorVersion")
            if not package_id or not version_raw:
                continue  # entrée incomplète, on ignore plutôt que de deviner

            component_type_el = None
            for child in element:
                if _local_name(child.tag) == "ComponentType":
                    component_type_el = child
                    break
            component_type_value = component_type_el.get("value") if component_type_el is not None else ""

            display_name = ""
            for child in element:
                if _local_name(child.tag) == "Name":
                    display_name = _find_child_text(child, "Display") or ""
                    break

            component_type = classify_dell_component(component_type_value, display_name)

            packages.append(
                FirmwarePackage(
                    component_type=component_type,
                    version=FirmwareVersion(version_raw),
                    package_id=package_id,
                    # Dell ne publie pas ces deux informations dans Catalog.xml :
                    # valeurs par défaut prudentes, à ajuster manuellement si
                    # une source plus précise (notes de version) est disponible.
                    reboot_required=True,
                    estimated_minutes=15,
                )
            )
        return packages
