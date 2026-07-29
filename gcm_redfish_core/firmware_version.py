"""Représentation et comparaison de versions de firmware.

Les constructeurs (HPE, Dell, Lenovo) n'utilisent pas un format de version
homogène : ``"2.36"``, ``"2.30.0"``, ``"1.40.13.0"``, parfois avec un
suffixe (``"2.36-rc1"``). Cette classe fournit une comparaison robuste sans
supposer un schéma SemVer strict.
"""

from __future__ import annotations

import re
from functools import total_ordering

_SEGMENT_RE = re.compile(r"\d+|[a-zA-Z]+")


@total_ordering
class FirmwareVersion:
    """Version de firmware comparable, tolérante aux formats hétérogènes.

    La chaîne d'origine est découpée en segments numériques/alphabétiques
    (ex. ``"2.36-rc1"`` -> ``[2, 36, "rc", 1]``). La comparaison se fait
    segment par segment ; un segment numérique est toujours considéré
    inférieur à un segment alphabétique à la même position (convention :
    une pré-version comme "rc1" est antérieure à la version finale), sauf
    si les deux segments sont du même type.

    Attributes:
        raw: Chaîne de version telle que fournie par le constructeur.
    """

    def __init__(self, raw: str) -> None:
        self.raw = raw.strip()
        self._segments: tuple[int | str, ...] = self._parse(self.raw)

    @staticmethod
    def _parse(raw: str) -> tuple[int | str, ...]:
        parts = _SEGMENT_RE.findall(raw)
        segments: list[int | str] = []
        for part in parts:
            segments.append(int(part) if part.isdigit() else part.lower())
        return tuple(segments)

    def _comparable_key(self, other: "FirmwareVersion") -> tuple:
        # Complète les segments manquants avec 0 pour comparer des longueurs
        # différentes (ex. "2.30" vs "2.30.1").
        length = max(len(self._segments), len(other._segments))
        a = list(self._segments) + [0] * (length - len(self._segments))
        b = list(other._segments) + [0] * (length - len(other._segments))
        norm_a, norm_b = [], []
        for x, y in zip(a, b):
            if type(x) is type(y):
                norm_a.append((0, x))
                norm_b.append((0, y))
            else:
                # int vs str à la même position : on force une comparaison
                # stable en typant explicitement (str < int par convention
                # ci-dessus, donc str -> rang 0, int -> rang 1).
                norm_a.append((0, x) if isinstance(x, str) else (1, x))
                norm_b.append((0, y) if isinstance(y, str) else (1, y))
        return tuple(norm_a), tuple(norm_b)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FirmwareVersion):
            return NotImplemented
        return self._segments == other._segments

    def __lt__(self, other: "FirmwareVersion") -> bool:
        if not isinstance(other, FirmwareVersion):
            return NotImplemented
        norm_a, norm_b = self._comparable_key(other)
        return norm_a < norm_b

    def __hash__(self) -> int:
        return hash(self._segments)

    def __repr__(self) -> str:
        return f"FirmwareVersion({self.raw!r})"

    def __str__(self) -> str:
        return self.raw
