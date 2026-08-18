# shellcheck shell=bash
SCENARIO_DESC="no limits at all, every reading should fall back to host values"
REQUIRES=""
INNER_STYLE=local

scenario_exec() { sh -c "$1"; }
