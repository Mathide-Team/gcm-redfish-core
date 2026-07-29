"""Firmware Knowledge Base (FKB) : règles de dépendances entre firmwares.

Cette base est volontairement séparée du moteur (``dependency_engine.py``)
et du reste de l'application : elle est pensée pour être chargée depuis un
fichier JSON versionné indépendamment, enrichi au fil du temps sans
publier une nouvelle version du logiciel.

Trois types de règles :

- Graphe de mise à jour (``upgrade_graph``) : pour un (constructeur, modèle,
  composant), quelles versions peuvent être flashées directement depuis
  quelle version. L'absence d'arête directe entre deux versions oblige à
  passer par un ou plusieurs paliers intermédiaires.
- Prérequis croisés (``PrerequisiteRule``) : une version cible d'un
  composant peut exiger qu'un autre composant soit déjà à une version
  minimale.
- Incompatibilités (``IncompatibilityRule``) : deux versions de composants
  différents qui ne doivent jamais coexister.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .firmware_version import FirmwareVersion

_OPERATORS = ("==", "!=", "<", "<=", ">", ">=")


@dataclass(frozen=True)
class VersionConstraint:
    """Contrainte de comparaison sur une version (ex. ``< 6.0``, ``>= 2.72``).

    Permet d'exprimer une règle sur toute une plage de versions plutôt que
    sur une version exacte (ex. "toute la branche SmartArray 5.x", pas
    seulement "5.90").
    """

    operator: str
    version: FirmwareVersion

    def __post_init__(self) -> None:
        if self.operator not in _OPERATORS:
            raise ValueError(
                f"Opérateur de contrainte inconnu : {self.operator!r} "
                f"(attendu l'un de {_OPERATORS})"
            )

    def matches(self, actual: FirmwareVersion | None) -> bool:
        if actual is None:
            return False
        if self.operator == "==":
            return actual == self.version
        if self.operator == "!=":
            return actual != self.version
        if self.operator == "<":
            return actual < self.version
        if self.operator == "<=":
            return actual <= self.version
        if self.operator == ">":
            return actual > self.version
        return actual >= self.version  # ">="

    def __str__(self) -> str:
        return f"{self.operator} {self.version}"

    @classmethod
    def from_entry(cls, version: str, operator: str = "==") -> "VersionConstraint":
        return cls(operator=operator, version=FirmwareVersion(version))


@dataclass(frozen=True)
class PrerequisiteRule:
    """Le composant ``target`` ne peut atteindre ``target_version`` que si
    ``requires`` est déjà à ``min_version`` ou plus.
    """

    vendor: str
    model: str
    target: str
    target_version: FirmwareVersion
    requires: str
    min_version: FirmwareVersion


@dataclass(frozen=True)
class IncompatibilityRule:
    """Deux plages de versions de composants différents qui ne doivent
    jamais coexister (ex. ``bios == 2.30`` avec ``raid < 6.0``)."""

    vendor: str
    model: str
    component_a: str
    constraint_a: VersionConstraint
    component_b: str
    constraint_b: VersionConstraint
    reason: str = ""

    def matches(
        self, version_a: FirmwareVersion | None, version_b: FirmwareVersion | None
    ) -> bool:
        return self.constraint_a.matches(version_a) and self.constraint_b.matches(version_b)


class KnowledgeBase:
    """Référentiel de règles de dépendances firmware, chargeable depuis JSON."""

    def __init__(self) -> None:
        # (vendor, model, component_type) -> {version_str: [version_str, ...]}
        self._graph: dict[tuple[str, str, str], dict[str, list[str]]] = {}
        self._prerequisites: list[PrerequisiteRule] = []
        self._incompatibilities: list[IncompatibilityRule] = []

    @classmethod
    def from_json_file(cls, path: str | Path) -> "KnowledgeBase":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeBase":
        kb = cls()
        for entry in data.get("upgrade_paths", []):
            kb.add_upgrade_edges(
                vendor=entry["vendor"],
                model=entry["model"],
                component_type=entry["component_type"],
                edges=entry["edges"],
            )
        for entry in data.get("prerequisites", []):
            kb.add_prerequisite(
                PrerequisiteRule(
                    vendor=entry["vendor"],
                    model=entry["model"],
                    target=entry["target"],
                    target_version=FirmwareVersion(entry["target_version"]),
                    requires=entry["requires"],
                    min_version=FirmwareVersion(entry["min_version"]),
                )
            )
        for entry in data.get("incompatibilities", []):
            kb.add_incompatibility(
                IncompatibilityRule(
                    vendor=entry["vendor"],
                    model=entry["model"],
                    component_a=entry["component_a"],
                    constraint_a=VersionConstraint.from_entry(
                        entry["version_a"], entry.get("operator_a", "==")
                    ),
                    component_b=entry["component_b"],
                    constraint_b=VersionConstraint.from_entry(
                        entry["version_b"], entry.get("operator_b", "==")
                    ),
                    reason=entry.get("reason", ""),
                )
            )
        return kb

    def add_upgrade_edges(
        self, vendor: str, model: str, component_type: str, edges: dict[str, list[str]]
    ) -> None:
        """Déclare, pour un composant, les transitions directes autorisées.

        Args:
            edges: dict ``{version_source: [versions_cible_directement_flashables]}``.
        """
        key = (vendor.lower(), model.lower(), component_type.lower())
        self._graph.setdefault(key, {})
        for src, dsts in edges.items():
            self._graph[key].setdefault(src, [])
            self._graph[key][src].extend(dsts)

    def add_prerequisite(self, rule: PrerequisiteRule) -> None:
        self._prerequisites.append(rule)

    def add_incompatibility(self, rule: IncompatibilityRule) -> None:
        self._incompatibilities.append(rule)

    def upgrade_graph(self, vendor: str, model: str, component_type: str) -> dict[str, list[str]]:
        return self._graph.get((vendor.lower(), model.lower(), component_type.lower()), {})

    def prerequisites_for(self, vendor: str, model: str, target: str) -> list[PrerequisiteRule]:
        return [
            r
            for r in self._prerequisites
            if r.vendor.lower() == vendor.lower()
            and r.model.lower() == model.lower()
            and r.target.lower() == target.lower()
        ]

    def incompatibilities_for(self, vendor: str, model: str) -> list[IncompatibilityRule]:
        return [
            r
            for r in self._incompatibilities
            if r.vendor.lower() == vendor.lower() and r.model.lower() == model.lower()
        ]
