#!/usr/bin/env bash
# inspect res_users logins/passwords in the masked DB.
exec bash "$(dirname "$0")/psql.sh" \
  "select id,login,left(password,20) as pw_prefix,active from res_users order by id limit 8;"
