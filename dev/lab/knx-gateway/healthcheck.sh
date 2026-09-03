#!/bin/sh
set -eu

ss -H -lun | awk '{print $4}' | grep -Eq '(^|:)3672$'
