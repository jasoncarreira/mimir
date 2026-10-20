#!/bin/sh
set -eu
groupadd --gid 1001 mimir
groupadd --gid 1002 worklink
useradd --create-home --uid 1001 --gid mimir --groups worklink --shell /bin/bash mimir
useradd --no-create-home --uid 1002 --gid worklink --home-dir /nonexistent --shell /usr/sbin/nologin worklink
chmod 0700 /home/mimir
install -d -o root -g root -m 0711 /var/lib/mimir-worklink
install -d -o root -g mimir -m 0771 /var/lib/mimir-worklink/checkouts
install -d -o root -g mimir -m 0771 /var/lib/mimir-worklink/repo-test-checkouts
install -d -o root -g mimir -m 0771 /var/lib/mimir-worklink/opencode-checkouts
install -d -o root -g worklink -m 0710 /var/lib/mimir-worklink/homes
