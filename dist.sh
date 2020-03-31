#!/bin/sh
pyinstaller --distpath . --specpath ./spec --name _myinit -F myinit.py
# pyinstaller --specpath ./spec myinit.py

