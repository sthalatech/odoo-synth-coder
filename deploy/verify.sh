#!/usr/bin/env bash
# verify the masked replica: row count + sample of masked res_partner/res_users.
exec bash "$(dirname "$0")/psql.sh" \
  "select count(*) as partners from res_partner; select id,name,email from res_partner order by id limit 6; select id,login,active from res_users order by id limit 6;"
