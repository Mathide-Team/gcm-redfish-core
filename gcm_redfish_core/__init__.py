"""gcm_redfish_core — bibliothèque indépendante de GTK.

Contient le client Redfish, l'import de catalogues constructeur (inventaire)
et les modèles de données, destinés à être réutilisés par le plugin GTK de
GCM comme par toute autre interface future (web, CLI).

Le Firmware Dependency Engine (paliers/prérequis) reste présent dans le
code mais n'est plus une priorité du projet : aucune source publique fiable
n'a été trouvée pour l'alimenter (voir /areas/redfish-plugin-gcm.md).
"""

from .catalog_import import CatalogImporter, DellCatalogImporter, HPESmartComponentImporter
from .dependency_engine import DependencyEngine, PlanIssue, Severity, Step, UpdatePlan
from .firmware_version import FirmwareVersion
from .knowledge_base import IncompatibilityRule, KnowledgeBase, PrerequisiteRule
from .models import ComponentState, FirmwarePackage, Repository, ServerProfile
from .redfish_client import RedfishAuthError, RedfishClient, RedfishError

__all__ = [
    "CatalogImporter",
    "DellCatalogImporter",
    "HPESmartComponentImporter",
    "DependencyEngine",
    "PlanIssue",
    "Severity",
    "Step",
    "UpdatePlan",
    "FirmwareVersion",
    "IncompatibilityRule",
    "KnowledgeBase",
    "PrerequisiteRule",
    "ComponentState",
    "FirmwarePackage",
    "Repository",
    "ServerProfile",
    "RedfishClient",
    "RedfishError",
    "RedfishAuthError",
]
