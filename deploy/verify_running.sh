#!/usr/bin/env bash
set -uo pipefail
R=us-east-1
echo "source log groups:"
aws logs describe-log-groups --region $R --query 'logGroups[?contains(logGroupName,`src`)||contains(logGroupName,`source`)].logGroupName' --output text

check() {
  CL=$1; SVC=$2; LG=$3
  t=$(aws ecs list-tasks --cluster "$CL" --service-name "$SVC" --region $R --query 'taskArns[0]' --output text)
  tid=${t##*/}
  echo "=== $SVC task=$tid ==="
  st=$(aws logs describe-log-streams --log-group-name "$LG" --region $R --query "logStreams[?contains(logStreamName,\`$tid\`)].logStreamName" --output text)
  echo "stream=$st"
  aws logs get-log-events --log-group-name "$LG" --log-stream-name "$st" --region $R --start-from-head --limit 12 --query 'events[].message' --output text 2>&1 \
    | tr '\t' '\n' | grep -iE "source commit|version 1|addons_path|HTTP service" | head -5
}

check odoo-synth        odoo-synth-odoo     /ecs/odoo-synth
check odoo-synth-source odoo-synth-src-odoo /ecs/odoo-synth-source
