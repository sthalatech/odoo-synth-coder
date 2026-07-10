#!/usr/bin/env bash
# 04: DB subnet group + RDS Postgres instance (holds source + masked DBs).
source "$(dirname "$0")/lib.sh"
: "${RDS_SG:?run 03_network.sh}"

SUBNETS="$(subnet_ids)"
GRP="$PROJECT-subnets"
aws rds describe-db-subnet-groups --db-subnet-group-name "$GRP" --region "$AWS_REGION" >/dev/null 2>&1 \
  || aws rds create-db-subnet-group --db-subnet-group-name "$GRP" \
       --db-subnet-group-description "odoo-synth" --subnet-ids $SUBNETS \
       --region "$AWS_REGION" >/dev/null

if ! aws rds describe-db-instances --db-instance-identifier "$RDS_INSTANCE_ID" \
      --region "$AWS_REGION" >/dev/null 2>&1; then
  log "creating RDS $RDS_INSTANCE_ID (pg$PG_MAJOR, $RDS_INSTANCE_CLASS) ..."
  aws rds create-db-instance \
    --db-instance-identifier "$RDS_INSTANCE_ID" \
    --db-instance-class "$RDS_INSTANCE_CLASS" \
    --engine postgres --engine-version "$PG_MAJOR" \
    --allocated-storage "$RDS_ALLOCATED_GB" \
    --master-username "$TARGET_DB_USER" \
    --master-user-password "$TARGET_DB_PASSWORD" \
    --db-subnet-group-name "$GRP" \
    --vpc-security-group-ids "$RDS_SG" \
    --no-multi-az --backup-retention-period 0 \
    --no-publicly-accessible \
    --region "$AWS_REGION" >/dev/null
fi

log "waiting for RDS available ..."
aws rds wait db-instance-available --db-instance-identifier "$RDS_INSTANCE_ID" --region "$AWS_REGION"
EP="$(aws rds describe-db-instances --db-instance-identifier "$RDS_INSTANCE_ID" \
  --region "$AWS_REGION" --query 'DBInstances[0].Endpoint.Address' --output text)"
put_state RDS_ENDPOINT "$EP"
log "RDS endpoint: $EP"
