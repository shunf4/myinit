#!/bin/sh
python3.7 -m PyInstaller --specpath ./spec --distpath . --name _myinit -F myinit.py

