#!/usr/bin/env sh -e

export GRAPHVIZ_DOT=/c/Users/u68320/AppData/Local/Continuum/miniconda3/envs/py36/Library/bin/graphviz/dot

java -Djava.awt.headless=true -jar /c/Users/u68320/Downloads/plantuml.jar -tsvg -failfast2 "$@"
