"""Firmware Dependency Engine.

Principe directeur (voir discussion projet) : ne JAMAIS proposer une mise à
jour automatique si le chemin ne peut pas être déterminé avec certitude à
partir de la Firmware Knowledge Base. Dans le doute, le moteur produit un
``PlanIssue`` de sévérité ``MANUAL_REVIEW`` ou ``BLOCKING`` plutôt que de
deviner un ordre d'exécution.

Ce module ne fait aucun appel réseau ni Redfish : il consomme un
``ServerProfile`` (état courant), un ``Repository`` (paquets disponibles)
et une ``KnowledgeBase`` (règles), et produit un ``UpdatePlan`` — la
simulation. L'exécution réelle (envoi des requêtes Redfish, attente des
tâches, redémarrages) est un module séparé qui consommera ce plan.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from .firmware_version import FirmwareVersion
from .knowledge_base import KnowledgeBase
from .models import Repository, ServerProfile


class Severity(str, Enum):
    """Sévérité d'un problème détecté lors de la simulation."""

    INFO = "info"
    MANUAL_REVIEW = "manual_review"  # nécessite validation d'un administrateur
    BLOCKING = "blocking"  # empêche toute exécution automatique


@dataclass(frozen=True)
class PlanIssue:
    """Problème détecté pendant la planification."""

    severity: Severity
    component_type: str
    message: str


@dataclass(frozen=True)
class Step:
    """Une étape unitaire d'exécution (une mise à jour de composant)."""

    component_type: str
    from_version: FirmwareVersion
    to_version: FirmwareVersion
    package_id: str
    reboot_required: bool
    estimated_minutes: int


@dataclass
class UpdatePlan:
    """Résultat de la simulation : plan d'exécution ordonné + diagnostics."""

    server_model: str
    steps: list[Step] = field(default_factory=list)
    issues: list[PlanIssue] = field(default_factory=list)

    @property
    def total_reboots(self) -> int:
        return sum(1 for s in self.steps if s.reboot_required)

    @property
    def total_estimated_minutes(self) -> int:
        return sum(s.estimated_minutes for s in self.steps)

    @property
    def has_blocking_issues(self) -> bool:
        return any(i.severity == Severity.BLOCKING for i in self.issues)

    @property
    def requires_manual_validation(self) -> bool:
        return any(
            i.severity in (Severity.BLOCKING, Severity.MANUAL_REVIEW) for i in self.issues
        )

    @property
    def is_safe_to_execute(self) -> bool:
        """True seulement si aucun problème bloquant ni incertitude détectée."""
        return not self.requires_manual_validation

    def summary(self) -> str:
        lines = [
            f"Plan pour {self.server_model} : {len(self.steps)} étape(s), "
            f"{self.total_reboots} redémarrage(s), ~{self.total_estimated_minutes} min",
        ]
        for step in self.steps:
            lines.append(
                f"  - {step.component_type}: {step.from_version} -> {step.to_version} "
                f"(paquet {step.package_id}, reboot={step.reboot_required})"
            )
        for issue in self.issues:
            lines.append(f"  [{issue.severity.value}] {issue.component_type}: {issue.message}")
        if not self.is_safe_to_execute:
            lines.append("  => VALIDATION MANUELLE REQUISE avant exécution.")
        return "\n".join(lines)


class DependencyEngine:
    """Calcule un plan de mise à jour sûr à partir d'une Knowledge Base."""

    def __init__(self, knowledge_base: KnowledgeBase, repository: Repository) -> None:
        self._kb = knowledge_base
        self._repo = repository

    # ------------------------------------------------------------------
    # Chemin de mise à jour pour un seul composant
    # ------------------------------------------------------------------
    def _component_path(
        self,
        vendor: str,
        model: str,
        component_type: str,
        current: FirmwareVersion,
        target: FirmwareVersion,
    ) -> tuple[list[Step], list[PlanIssue]]:
        issues: list[PlanIssue] = []

        if current == target:
            return [], issues

        graph_raw = self._kb.upgrade_graph(vendor, model, component_type)
        if not graph_raw:
            issues.append(
                PlanIssue(
                    Severity.MANUAL_REVIEW,
                    component_type,
                    f"Aucune règle de dépendance connue pour {component_type} sur {model} "
                    f"({current} -> {target}) : chemin non garanti, validation requise.",
                )
            )
            return [], issues

        # Construit le graphe en objets FirmwareVersion pour une comparaison
        # tolérante aux formats (évite les faux négatifs de correspondance
        # de chaînes).
        nodes: dict[str, FirmwareVersion] = {}
        for src in graph_raw:
            nodes.setdefault(src, FirmwareVersion(src))
            for dst in graph_raw[src]:
                nodes.setdefault(dst, FirmwareVersion(dst))

        def find_node(v: FirmwareVersion) -> str | None:
            for key, val in nodes.items():
                if val == v:
                    return key
            return None

        start_key = find_node(current)
        goal_key = find_node(target)

        if start_key is None or goal_key is None:
            issues.append(
                PlanIssue(
                    Severity.MANUAL_REVIEW,
                    component_type,
                    f"Version courante ou cible ({current} -> {target}) absente du graphe "
                    f"de dépendances connu pour {component_type} sur {model} : "
                    "chemin non garanti, validation requise.",
                )
            )
            return [], issues

        # BFS (chemin le plus court en nombre de paliers).
        parent: dict[str, str] = {}
        queue: deque[str] = deque([start_key])
        visited = {start_key}
        while queue:
            node = queue.popleft()
            if node == goal_key:
                break
            for nxt in graph_raw.get(node, []):
                if nxt not in visited:
                    visited.add(nxt)
                    parent[nxt] = node
                    queue.append(nxt)

        if goal_key not in visited:
            issues.append(
                PlanIssue(
                    Severity.BLOCKING,
                    component_type,
                    f"Aucun chemin de mise à jour validé de {current} vers {target} "
                    f"pour {component_type} sur {model}.",
                )
            )
            return [], issues

        # Reconstruction du chemin.
        path_keys = [goal_key]
        while path_keys[-1] != start_key:
            path_keys.append(parent[path_keys[-1]])
        path_keys.reverse()

        steps: list[Step] = []
        for src_key, dst_key in zip(path_keys, path_keys[1:]):
            dst_version = nodes[dst_key]
            package = self._repo.find(component_type, dst_version)
            if package is None:
                issues.append(
                    PlanIssue(
                        Severity.MANUAL_REVIEW,
                        component_type,
                        f"Palier {dst_version} requis pour {component_type} mais aucun "
                        "paquet correspondant n'est disponible dans le dépôt.",
                    )
                )
                # On garde une étape "placeholder" pour la visibilité du plan,
                # mais l'absence de paquet la rend non exécutable telle quelle.
                package_id = "<paquet manquant>"
                reboot_required = True
                estimated_minutes = 10
            else:
                package_id = package.package_id
                reboot_required = package.reboot_required
                estimated_minutes = package.estimated_minutes
            steps.append(
                Step(
                    component_type=component_type,
                    from_version=nodes[src_key],
                    to_version=dst_version,
                    package_id=package_id,
                    reboot_required=reboot_required,
                    estimated_minutes=estimated_minutes,
                )
            )
        return steps, issues

    # ------------------------------------------------------------------
    # Plan global multi-composants
    # ------------------------------------------------------------------
    def plan_update(
        self, profile: ServerProfile, targets: dict[str, FirmwareVersion]
    ) -> UpdatePlan:
        """Simule une campagne de mise à jour pour un serveur.

        Args:
            profile: État courant du serveur.
            targets: Versions cibles souhaitées par type de composant.

        Returns:
            Un ``UpdatePlan`` ordonné avec les problèmes détectés. Ne
            lance jamais aucune action réelle.
        """
        plan = UpdatePlan(server_model=f"{profile.vendor} {profile.model}")
        component_chains: dict[str, list[Step]] = {}

        for component_type, target_version in targets.items():
            current = profile.current_version(component_type)
            if current is None:
                plan.issues.append(
                    PlanIssue(
                        Severity.MANUAL_REVIEW,
                        component_type,
                        "Version courante inconnue (absente du profil serveur) : "
                        "impossible de garantir le chemin de mise à jour.",
                    )
                )
                continue
            steps, issues = self._component_path(
                profile.vendor, profile.model, component_type, current, target_version
            )
            component_chains[component_type] = steps
            plan.issues.extend(issues)

        # Prérequis croisés : ordonnancement global respectant les chaînes
        # internes à chaque composant + les contraintes entre composants.
        ordered_steps = self._order_with_prerequisites(profile, component_chains, plan)
        plan.steps = ordered_steps

        # Incompatibilités sur l'état final.
        self._check_incompatibilities(profile, targets, plan)

        return plan

    def _final_version(
        self, profile: ServerProfile, component_type: str, chains: dict[str, list[Step]]
    ) -> FirmwareVersion | None:
        chain = chains.get(component_type)
        if chain:
            return chain[-1].to_version
        return profile.current_version(component_type)

    def _order_with_prerequisites(
        self,
        profile: ServerProfile,
        chains: dict[str, list[Step]],
        plan: UpdatePlan,
    ) -> list[Step]:
        # Représente chaque étape par un identifiant unique (component, index).
        node_ids: list[tuple[str, int]] = [
            (comp, i) for comp, steps in chains.items() for i in range(len(steps))
        ]
        predecessors: dict[tuple[str, int], set[tuple[str, int]]] = {n: set() for n in node_ids}

        # Contraintes de chaîne (ordre interne à un composant).
        for comp, steps in chains.items():
            for i in range(1, len(steps)):
                predecessors[(comp, i)].add((comp, i - 1))

        # Contraintes croisées issues des prérequis de la KB.
        for comp, steps in chains.items():
            if not steps:
                continue
            for rule in self._kb.prerequisites_for(profile.vendor, profile.model, comp):
                target_idx = next(
                    (i for i, s in enumerate(steps) if s.to_version == rule.target_version),
                    None,
                )
                if target_idx is None:
                    continue  # ce chemin ne passe pas par le palier concerné par la règle

                current_req = profile.current_version(rule.requires)
                if current_req is not None and current_req >= rule.min_version:
                    continue  # déjà satisfait, rien à ordonner

                req_steps = chains.get(rule.requires, [])
                req_idx = next(
                    (i for i, s in enumerate(req_steps) if s.to_version >= rule.min_version),
                    None,
                )
                if req_idx is None:
                    plan.issues.append(
                        PlanIssue(
                            Severity.BLOCKING,
                            comp,
                            f"{comp} -> {rule.target_version} exige {rule.requires} "
                            f">= {rule.min_version}, mais aucune mise à jour prévue de "
                            f"{rule.requires} ne l'atteint.",
                        )
                    )
                    continue
                predecessors[(comp, target_idx)].add((rule.requires, req_idx))

        # Tri topologique (Kahn).
        remaining = set(node_ids)
        ordered: list[tuple[str, int]] = []
        while remaining:
            ready = sorted(n for n in remaining if predecessors[n] <= set(ordered))
            if not ready:
                plan.issues.append(
                    PlanIssue(
                        Severity.BLOCKING,
                        "*",
                        "Cycle de dépendances détecté entre composants : "
                        "aucun ordre d'exécution valide.",
                    )
                )
                break
            for n in ready:
                ordered.append(n)
                remaining.discard(n)

        return [chains[comp][i] for comp, i in ordered]

    def _check_incompatibilities(
        self,
        profile: ServerProfile,
        targets: dict[str, FirmwareVersion],
        plan: UpdatePlan,
    ) -> None:
        finals: dict[str, FirmwareVersion] = {}
        for comp in set(list(targets.keys()) + list(profile.components.keys())):
            current = profile.current_version(comp)
            finals[comp] = targets.get(comp, current) if current is not None else targets.get(comp)

        for rule in self._kb.incompatibilities_for(profile.vendor, profile.model):
            if rule.matches(finals.get(rule.component_a), finals.get(rule.component_b)):
                plan.issues.append(
                    PlanIssue(
                        Severity.BLOCKING,
                        f"{rule.component_a}+{rule.component_b}",
                        f"Combinaison incompatible : {rule.component_a} {rule.constraint_a} "
                        f"avec {rule.component_b} {rule.constraint_b}. {rule.reason}".strip(),
                    )
                )
