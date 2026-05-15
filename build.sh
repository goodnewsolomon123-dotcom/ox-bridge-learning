#!/usr/bin/env bash
# Exit on error
set -o errexit

# Install Python 3.11
pip install pip==23.3.1
pip install -r requirements.txt
