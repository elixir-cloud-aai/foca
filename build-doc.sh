#!/usr/bin/env bash
SOURCE_CODE_PATH=$1
rm -Rf docs/build
rm -Rf docs/source/doc_files
sphinx-apidoc -f -o docs/source/doc_files/ $SOURCE_CODE_PATH
cd docs && make html && cd ..