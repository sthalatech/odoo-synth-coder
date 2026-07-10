#!/usr/bin/env bash
# SOURCE stack 02: dedicated RDS Postgres for the source database.
source "$(dirname "$0")/../lib.sh"
: "${SRC_RDS_SG:?run source/01_network.sh}"

SUBNETS="$(subnet_ids)"
GRP="$PROJECT-src-subnets"
aws rds describe-db-subnet-groups --db-subnet-group-name "$GRP" --region "$AWS_REGION" >/dev/null 2>&1 \
  || aws rds create-db-subnet-group --db-subnet-group-name "$GRP" \
       --db-subnet-group-description "odoo-synth source" --subnet-ids $SUBNETS \
       --region "$AWS_REGION" >/dev/null

if ! aws rds describe-db-instances --db-instance-identifier "$SOURCE_RDS_INSTANCE_ID" \
      --region "$AWS_REGION" >/dev/null 2>&1; then
  log "creating SOURCE RDS $SOURCE_RDS_INSTANCE_ID (pg$PG_MAJOR, $RDS_INSTANCE_CLASS) ..."
  aws rds create-db-instance \
    --db-instance-identifier "$SOURCE_RDS_INSTANCE_ID" \
    --db-instance-class "$RDS_INSTANCE_CLASS" \
    --engine postgres --engine-version "$PG_MAJOR" \
    --allocated-storage "$RDS_ALLOCATED_GB" \
    --master-username "$SOURCE_DB_MASTER_USER" \
    --master-user-password "$SOURCE_DB_MASTER_PASSWORD" \
    --db-subnet-group-name "$GRP" \
    --vpc-security-group-ids "$SRC_RDS_SG" \
    --no-multi-az --backup-retention-period 0 \
    --no-publicly-accessible \
    --region "$AWS_REGION" >/dev/null
fi

log "waiting for SOURCE RDS available ..."
aws rds wait db-instance-available --db-instance-identifier "$SOURCE_RDS_INSTANCE_ID" --region "$AWS_REGION"
EP="$(aws rds describe-db-instances --db-instance-identifier "$SOURCE_RDS_INSTANCE_ID" \
  --region "$AWS_REGION" --query 'DBInstances[0].Endpoint.Address' --output text)"
put_state SRC_RDS_ENDPOINT "$EP"
log "SOURCE RDS endpoint: $EP"
