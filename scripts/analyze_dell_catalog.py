#!/usr/bin/env python3
"""analyze_dell_catalog.py

Analyse un catalogue Dell (``Catalog.xml``) complet : classe chaque
composant avec ``classify_dell_component``, affiche des statistiques de
couverture, et écrit un fichier XML contenant uniquement les composants
non classés avec confiance — pour revue humaine et enrichissement des
mots-clés dans ``catalog_import.py``.

À exécuter sur TA machine (le sandbox Claude n'a pas d'accès réseau pour
télécharger le catalogue complet). Nécessite le package ``gcm_redfish_core``
à côté de ce script (même dossier, ou installé). Dans ce dépôt, le
script suppose que ``gcm_redfish_core/`` se trouve au niveau racine,
un dossier au-dessus de ``scripts/``.

Usage :
    # Télécharge le catalogue officiel puis l'analyse
    python3 analyze_dell_catalog.py --download

    # Analyse un catalogue déjà téléchargé/décompressé
    python3 analyze_dell_catalog.py --catalog-path Catalog.xml

Sorties (dans --out-dir, défaut ./dell_catalog_analysis) :
    stats.txt                  résumé lisible
    unclassified_components.xml   les <SoftwareComponent> non classés, tels
                                   quels, prêts à être ouverts/annotés
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

# Permet d'exécuter le script depuis le dossier qui contient gcm_redfish_core/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gcm_redfish_core.catalog_import import (  # noqa: E402
    classify_dell_component,
    is_confident_dell_classification,
)

CATALOG_URL = "https://downloads.dell.com/catalog/Catalog.xml.gz"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def download_catalog(out_dir: Path) -> Path:
    gz_path = out_dir / "Catalog.xml.gz"
    xml_path = out_dir / "Catalog.xml"
    print(f"Téléchargement de {CATALOG_URL} ...")
    urllib.request.urlretrieve(CATALOG_URL, gz_path)
    print("Décompression ...")
    with gzip.open(gz_path, "rb") as f_in, open(xml_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    print(f"Catalogue complet : {xml_path} ({xml_path.stat().st_size / 1_048_576:.1f} Mo)")
    return xml_path


def component_fields(element: ET.Element) -> tuple[str, str, str, str]:
    """Retourne (component_type_brut, name, package_id, version)."""
    component_type = ""
    name = ""
    for child in element:
        tag = local_name(child.tag)
        if tag == "ComponentType":
            component_type = child.get("value", "")
        elif tag == "Name":
            for grandchild in child:
                if local_name(grandchild.tag) == "Display" and grandchild.text:
                    name = grandchild.text.strip()
                    break
    package_id = element.get("packageID") or element.get("releaseID") or ""
    version = element.get("dellVersion") or element.get("vendorVersion") or ""
    return component_type, name, package_id, version


def analyze(catalog_path: Path, out_dir: Path) -> None:
    print(f"Analyse de {catalog_path} ...")
    tree = ET.parse(catalog_path)
    root = tree.getroot()

    total = 0
    resolved_counts: Counter[str] = Counter()
    raw_type_counts_when_unclassified: Counter[str] = Counter()
    unclassified_elements: list[ET.Element] = []

    for element in root.iter():
        if local_name(element.tag) != "SoftwareComponent":
            continue
        total += 1
        component_type, name, package_id, version = component_fields(element)
        resolved = classify_dell_component(component_type, name)
        resolved_counts[resolved] += 1
        if not is_confident_dell_classification(resolved):
            raw_type_counts_when_unclassified[component_type or "(vide)"] += 1
            unclassified_elements.append(element)

    confident_total = sum(
        n for cat, n in resolved_counts.items() if is_confident_dell_classification(cat)
    )
    unclassified_total = total - confident_total
    coverage_pct = (confident_total / total * 100) if total else 0.0

    lines = []
    lines.append(f"Total <SoftwareComponent> analysés : {total}")
    lines.append(f"Classés avec confiance : {confident_total} ({coverage_pct:.1f}%)")
    lines.append(f"Non classés (repli sur le type Dell brut) : {unclassified_total}")
    lines.append("")
    lines.append("Répartition par catégorie résolue :")
    for cat, n in resolved_counts.most_common():
        marker = "" if is_confident_dell_classification(cat) else "  <- repli brut"
        lines.append(f"  {cat}: {n}{marker}")
    lines.append("")
    lines.append("Détail des non-classés par ComponentType Dell d'origine :")
    for raw_type, n in raw_type_counts_when_unclassified.most_common():
        lines.append(f"  {raw_type}: {n}")

    report = "\n".join(lines)
    print()
    print(report)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "stats.txt").write_text(report + "\n", encoding="utf-8")

    unclassified_root = ET.Element("Manifest")
    unclassified_root.set("source_total_components", str(total))
    unclassified_root.set("unclassified_count", str(unclassified_total))
    for element in unclassified_elements:
        unclassified_root.append(element)
    ET.ElementTree(unclassified_root).write(
        out_dir / "unclassified_components.xml", encoding="UTF-8", xml_declaration=True
    )

    print()
    print(f"Rapport : {out_dir / 'stats.txt'}")
    print(f"XML des non-classés (pour expertise) : {out_dir / 'unclassified_components.xml'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog-path", type=Path, default=None, help="Chemin vers un Catalog.xml déjà présent"
    )
    parser.add_argument(
        "--download", action="store_true", help="Télécharger le catalogue officiel Dell"
    )
    parser.add_argument(
        "--out-dir", type=Path, default=Path("./dell_catalog_analysis"),
        help="Dossier de sortie (stats.txt + unclassified_components.xml)",
    )
    args = parser.parse_args()

    if args.catalog_path:
        catalog_path = args.catalog_path
    elif args.download:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        catalog_path = download_catalog(args.out_dir)
    else:
        parser.error("Fournis --catalog-path <fichier> ou --download")
        return

    analyze(catalog_path, args.out_dir)


if __name__ == "__main__":
    main()
