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

from loguru import logger

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
        logger.debug("__post_init__() called | opérateur={}", self.operator)
        if self.operator not in _OPERATORS:
            logger.error("opérateur de contrainte inconnu : {}", self.operator)
            raise ValueError(
                f"Opérateur de contrainte inconnu : {self.operator!r} "
                f"(attendu l'un de {_OPERATORS})"
            )
        logger.debug("__post_init__() returning | opérateur valide")

    def matches(self, actual: FirmwareVersion | None) -> bool:
        logger.debug("matches() called | opérateur={} version={}", self.operator, self.version)
        if actual is None:
            logger.debug("matches() returning | False (version réelle absente)")
            return False
        if self.operator == "==":
            resultat = actual == self.version
        elif self.operator == "!=":
            resultat = actual != self.version
        elif self.operator == "<":
            resultat = actual < self.version
        elif self.operator == "<=":
            resultat = actual <= self.version
        elif self.operator == ">":
            resultat = actual > self.version
        else:
            resultat = actual >= self.version  # ">="
        logger.debug("matches() returning | {}", resultat)
        return resultat

    def __str__(self) -> str:
        return f"{self.operator} {self.version}"

    @classmethod
    def from_entry(cls, version: str, operator: str = "==") -> "VersionConstraint":
        logger.debug("from_entry() called | version={} opérateur={}", version, operator)
        contrainte = cls(operator=operator, version=FirmwareVersion(version))
        logger.debug("from_entry() returning | contrainte construite")
        return contrainte


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
        logger.debug("IncompatibilityRule.matches() called")
        resultat = self.constraint_a.matches(version_a) and self.constraint_b.matches(version_b)
        logger.debug("IncompatibilityRule.matches() returning | {}", resultat)
        return resultat


class KnowledgeBase:
    """Référentiel de règles de dépendances firmware, chargeable depuis JSON."""

    def __init__(self) -> None:
        logger.debug("KnowledgeBase.__init__() called")
        # (vendor, model, component_type) -> {version_str: [version_str, ...]}
        self._graph: dict[tuple[str, str, str], dict[str, list[str]]] = {}
        self._prerequisites: list[PrerequisiteRule] = []
        self._incompatibilities: list[IncompatibilityRule] = []
        logger.debug("KnowledgeBase.__init__() returning")

    @classmethod
    def from_json_file(cls, path: str | Path) -> "KnowledgeBase":
        logger.debug("from_json_file() called | chemin={}", path)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        base = cls.from_dict(data)
        logger.debug("from_json_file() returning | base chargée")
        return base

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeBase":
        logger.debug(
            "from_dict() called | {} chemin(s), {} prérequis, {} incompatibilité(s)",
            len(data.get("upgrade_paths", [])),
            len(data.get("prerequisites", [])),
            len(data.get("incompatibilities", [])),
        )
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
        logger.debug("from_dict() returning | base construite")
        return kb

    def add_upgrade_edges(
        self, vendor: str, model: str, component_type: str, edges: dict[str, list[str]]
    ) -> None:
        """Déclare, pour un composant, les transitions directes autorisées.

        Args:
            edges: dict ``{version_source: [versions_cible_directement_flashables]}``.
        """
        logger.debug(
            "add_upgrade_edges() called | modèle={} composant={} arête(s)={}",
            model,
            component_type,
            len(edges),
        )
        key = (vendor.lower(), model.lower(), component_type.lower())
        self._graph.setdefault(key, {})
        for src, dsts in edges.items():
            self._graph[key].setdefault(src, [])
            self._graph[key][src].extend(dsts)
        logger.debug("add_upgrade_edges() returning")

    def add_prerequisite(self, rule: PrerequisiteRule) -> None:
        logger.debug("add_prerequisite() called | cible={}", rule.target)
        self._prerequisites.append(rule)
        logger.debug("add_prerequisite() returning | {} au total", len(self._prerequisites))

    def add_incompatibility(self, rule: IncompatibilityRule) -> None:
        logger.debug("add_incompatibility() called | composant_a={}", rule.component_a)
        self._incompatibilities.append(rule)
        logger.debug("add_incompatibility() returning | {} au total", len(self._incompatibilities))

    def upgrade_graph(self, vendor: str, model: str, component_type: str) -> dict[str, list[str]]:
        logger.debug(
            "upgrade_graph() called | modèle={} composant={}", model, component_type
        )
        graphe = self._graph.get((vendor.lower(), model.lower(), component_type.lower()), {})
        logger.debug("upgrade_graph() returning | {} version(s) source", len(graphe))
        return graphe

    def prerequisites_for(self, vendor: str, model: str, target: str) -> list[PrerequisiteRule]:
        logger.debug("prerequisites_for() called | modèle={} cible={}", model, target)
        regles = [
            r
            for r in self._prerequisites
            if r.vendor.lower() == vendor.lower()
            and r.model.lower() == model.lower()
            and r.target.lower() == target.lower()
        ]
        logger.debug("prerequisites_for() returning | {} règle(s)", len(regles))
        return regles

    def incompatibilities_for(self, vendor: str, model: str) -> list[IncompatibilityRule]:
        logger.debug("incompatibilities_for() called | modèle={}", model)
        regles = [
            r
            for r in self._incompatibilities
            if r.vendor.lower() == vendor.lower() and r.model.lower() == model.lower()
        ]
        logger.debug("incompatibilities_for() returning | {} règle(s)", len(regles))
        return regles
