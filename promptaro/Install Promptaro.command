#!/bin/bash
# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
# Installs Promptaro.app into your Applications folder — Author: Akshay Dubey
cd "$(dirname "$0")" && ./packaging/make-mac-app.sh && read -n1 -p "Done. Press any key to close."
