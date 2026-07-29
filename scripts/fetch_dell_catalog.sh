#!/usr/bin/env bash
# fetch_dell_catalog.sh
#
# À exécuter sur TA machine (pas dans le sandbox Claude, qui n'a pas
# d'accès réseau). Télécharge le catalogue officiel Dell et le décompresse
# en XML lisible, prêt à être uploadé pour analyse.
#
# Usage : ./fetch_dell_catalog.sh [dossier_de_sortie]

set -euo pipefail

OUT_DIR="${1:-./dell_catalog}"
URL="https://downloads.dell.com/catalog/Catalog.xml.gz"

mkdir -p "$OUT_DIR"
echo "Téléchargement de $URL ..."
curl -fSL "$URL" -o "$OUT_DIR/Catalog.xml.gz"

echo "Décompression ..."
gunzip -kf "$OUT_DIR/Catalog.xml.gz"

echo "Fichier prêt : $OUT_DIR/Catalog.xml"
echo "Taille : $(du -h "$OUT_DIR/Catalog.xml" | cut -f1)"
echo
echo "Attention : ce fichier peut faire plusieurs dizaines/centaines de Mo."
echo "Pour un échantillon exploitable en upload, tu peux en extraire un"
echo "sous-ensemble avec, par exemple :"
echo "  xmllint --xpath '(//SoftwareComponent)[position()<=50]' \"$OUT_DIR/Catalog.xml\" > \"$OUT_DIR/Catalog_sample.xml\""
echo "(nécessite libxml2-utils : apt install libxml2-utils / dnf install libxml2)"
