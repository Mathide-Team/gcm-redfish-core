#!/usr/bin/env python3
"""fetch_dell_catalog.py

À exécuter sur TA machine (pas dans le sandbox Claude, qui n'a pas
d'accès réseau). Télécharge le catalogue officiel Dell, le décompresse,
et en extrait un échantillon léger (les N premiers <SoftwareComponent>)
prêt à être uploadé pour analyse — le fichier complet fait souvent
plusieurs dizaines de Mo, inutile de tout envoyer.

Usage :
    python3 fetch_dell_catalog.py [--sample-size 50] [--out ./dell_catalog]
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

URL = "https://downloads.dell.com/catalog/Catalog.xml.gz"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=50)
    parser.add_argument("--out", default="./dell_catalog")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    gz_path = out_dir / "Catalog.xml.gz"
    xml_path = out_dir / "Catalog.xml"
    sample_path = out_dir / "Catalog_sample.xml"

    print(f"Téléchargement de {URL} ...")
    urllib.request.urlretrieve(URL, gz_path)

    print("Décompression ...")
    with gzip.open(gz_path, "rb") as f_in, open(xml_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    print(f"Fichier complet : {xml_path} ({xml_path.stat().st_size / 1_048_576:.1f} Mo)")

    print(f"Extraction d'un échantillon de {args.sample_size} composants ...")
    tree = ET.parse(xml_path)
    root = tree.getroot()
    sample_root = ET.Element("Manifest")
    count = 0
    for element in root.iter():
        if local_name(element.tag) != "SoftwareComponent":
            continue
        sample_root.append(element)
        count += 1
        if count >= args.sample_size:
            break

    ET.ElementTree(sample_root).write(sample_path, encoding="UTF-8", xml_declaration=True)
    print(f"Échantillon prêt à uploader : {sample_path} ({count} composants)")


if __name__ == "__main__":
    main()
