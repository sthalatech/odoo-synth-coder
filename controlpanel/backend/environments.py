"""Developer environment lifecycle via boto3 (EC2 + Secrets Manager).

An *environment* is an ephemeral, isolated VS Code (code-server) box launched
from a pre-baked golden AMI and seeded from a masked pg_dump artifact. One EC2
instance per environment; torn down on teardown (terminate instance + delete
the per-env secret). Designed to later be driven by GitHub-issue webhooks.
"""
from __future__ import annotations
import base64
import gzip
import secrets
import string
import time
import uuid
from pathlib import Path
from typing import Optional

import boto3

from . import config, store

TEMPLATE = Path(__file__).resolve().parent.parent / "environments" / "user-data.sh.tmpl"


def _region() -> str:
    return config.require("AWS_REGION")


def _gen_password(n: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _render_user_data(env_id: str, issue: str, dump_s3_uri: str,
                      secret_arn: str, odoo_image: str, repo_url: str,
                      repo_branch: str, git_token_secret: str, s: dict,
                      odoo_conf_extra: str = "", ssh_public_key: str = "") -> str:
    tmpl = TEMPLATE.read_text()
    conf_extra_b64 = base64.b64encode(
        (odoo_conf_extra or "").encode("utf-8")).decode("ascii")
    # per-env SSH key (from the UI) takes precedence over any configured default.
    pubkey = (ssh_public_key or s.get("ssh_public_key") or "").strip()
    ssh_pubkey_b64 = base64.b64encode(pubkey.encode("utf-8")).decode("ascii")
    repl = {
        "__ENV_ID__": env_id,
        "__ISSUE__": issue or "",
        "__AWS_REGION__": _region(),
        "__DUMP_S3_URI__": dump_s3_uri or "",
        "__SECRET_ARN__": secret_arn,
        "__ODOO_IMAGE__": odoo_image or "",
        "__REPO_URL__": repo_url or "",
        "__REPO_BRANCH__": repo_branch or "",
        "__GIT_TOKEN_SECRET__": git_token_secret or "",
        "__DB_NAME__": s.get("db_name") or "odoo",
        "__CODE_PORT__": s.get("code_port") or "8443",
        "__ODOO_PORT__": s.get("odoo_port") or "8069",
        "__ODOO_MASTER_PASSWORD__": config.get("ODOO_MASTER_PASSWORD", "change_me_master"),
        "__ODOO_CONF_EXTRA_B64__": conf_extra_b64,
        "__SSH_USER__": s.get("ssh_user") or "dev",
        "__SSH_PUBLIC_KEY_B64__": ssh_pubkey_b64,
    }
    for k, v in repl.items():
        tmpl = tmpl.replace(k, v)
    return tmpl


def _create_secret(env_id: str, password: str, s: dict) -> str:
    sm = boto3.client("secretsmanager", region_name=_region())
    name = f"{s.get('secret_prefix', 'odoo-synth/env')}/{env_id}"
    resp = sm.create_secret(
        Name=name,
        SecretString=password,
        Description=f"code-server password for odoo-synth env {env_id}",
    )
    return resp["ARN"]


def _delete_secret(secret_arn: str) -> None:
    try:
        sm = boto3.client("secretsmanager", region_name=_region())
        sm.delete_secret(SecretId=secret_arn, ForceDeleteWithoutRecovery=True)
    except Exception:  # noqa: BLE001
        pass


def _dump_uri_for_run(run_id: Optional[str], explicit: Optional[str]) -> Optional[str]:
    """Resolve the masked-dump S3 URI: explicit wins, else derive from the run's
    result (produce_dump stores a presigned GET; we prefer a plain s3:// URI the
    instance role can read, recorded on the run result as masked_dump_s3_uri)."""
    if explicit:
        return explicit
    if not run_id:
        return None
    run = store.get_run(run_id)
    if not run:
        return None
    res = run.get("result") or {}
    return res.get("masked_dump_s3_uri")


def _resolve_odoo_image(source_run_id: Optional[str], s: dict) -> Optional[str]:
    """The provenance-baked Odoo image the env should run. A run's own image
    (captured at mask time) wins so the code matches the masked data exactly;
    otherwise fall back to the configured image, resolving the account id if the
    config helper could not (no AWS_ACCOUNT_ID in env)."""
    if source_run_id:
        run = store.get_run(source_run_id)
        if run:
            img = (run.get("result") or {}).get("odoo_image")
            if img:
                return img
    if s.get("odoo_image"):
        return s["odoo_image"]
    proj = config.get("PROJECT")
    e = config.environments_cfg()
    tag = e.get("odoo_image_tag", "latest")
    if proj:
        try:
            acct = boto3.client("sts", region_name=_region()).get_caller_identity()["Account"]
            return f"{acct}.dkr.ecr.{_region()}.amazonaws.com/{proj}/odoo:{tag}"
        except Exception:  # noqa: BLE001
            return None
    return None


def _authorize_ip(ec2, sg_id: str, ip: str, ports) -> None:
    """Authorize a single IP (a.b.c.d or CIDR) on the env SG for the given TCP
    ports. Idempotent: an already-present rule is treated as success."""
    cidr = ip.strip()
    if not cidr:
        return
    if "/" not in cidr:
        cidr = f"{cidr}/32"
    perms = [{
        "IpProtocol": "tcp", "FromPort": int(p), "ToPort": int(p),
        "IpRanges": [{"CidrIp": cidr, "Description": "odoo-synth-env user access"}],
    } for p in ports]
    try:
        ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=perms)
    except Exception as exc:  # noqa: BLE001
        if "InvalidPermission.Duplicate" not in str(exc):
            raise


def _revoke_ip(sg_id: str, ip: str, ports) -> None:
    cidr = ip.strip()
    if not cidr:
        return
    if "/" not in cidr:
        cidr = f"{cidr}/32"
    perms = [{
        "IpProtocol": "tcp", "FromPort": int(p), "ToPort": int(p),
        "IpRanges": [{"CidrIp": cidr}],
    } for p in ports]
    try:
        ec2 = boto3.client("ec2", region_name=_region())
        ec2.revoke_security_group_ingress(GroupId=sg_id, IpPermissions=perms)
    except Exception:  # noqa: BLE001
        pass


def launch(env_id: str, source_run_id: Optional[str], issue: Optional[str],
           dump_s3_uri: Optional[str], repo_url: Optional[str],
           repo_branch: Optional[str], profile_id: Optional[str] = None,
           allow_ip: Optional[str] = None,
           ssh_public_key: Optional[str] = None) -> None:
    """Background worker: create secret, launch instance, poll until reachable."""
    s = config.environments_settings()
    try:
        if not config.environments_configured():
            raise RuntimeError(
                "developer environments are not configured (set ENV_AMI_ID, "
                "ENV_SG_ID, and the other environments.* values)")

        profile = store.get_profile(profile_id) if profile_id else None

        dump = _dump_uri_for_run(source_run_id, dump_s3_uri)
        if profile:
            # profile drives the image + addons so code matches the masked data.
            odoo_img = profile.get("image_uri") or _resolve_odoo_image(source_run_id, s)
            r_url = repo_url or profile.get("addons_git_url") or s.get("repo_url")
            r_branch = repo_branch or profile.get("addons_git_ref") or s.get("repo_branch")
            git_token_secret = profile.get("git_token_secret") or s.get("git_token_secret")
        else:
            odoo_img = _resolve_odoo_image(source_run_id, s)
            r_url = repo_url or s.get("repo_url")
            r_branch = repo_branch or s.get("repo_branch")
            git_token_secret = s.get("git_token_secret")
        store.update_environment(
            env_id, status="provisioning", dump_s3_uri=dump,
            odoo_image=odoo_img, repo_url=r_url, repo_branch=r_branch,
        )

        password = _gen_password()
        secret_arn = _create_secret(env_id, password, s)
        store.update_environment(env_id, secret_arn=secret_arn)

        conf_extra = (profile.get("odoo_conf_extra") if profile else "") or ""
        user_data = _render_user_data(
            env_id, issue or "", dump or "", secret_arn, odoo_img or "",
            r_url or "", r_branch or "", git_token_secret or "", s,
            odoo_conf_extra=conf_extra, ssh_public_key=ssh_public_key or "",
        )
        # EC2 caps user-data at 16384 bytes (pre-base64). cloud-init transparently
        # decompresses gzip'd user-data, so compress to make room for the SSH key
        # and other injected config.
        user_data_bytes = gzip.compress(user_data.encode("utf-8"))

        ec2 = boto3.client("ec2", region_name=_region())

        # Per-env firewall: open the SG to the user's IP for Odoo, code-server
        # and SSH (Remote-SSH deep link needs port 22). Recorded so teardown
        # can revoke it.
        if allow_ip:
            ports = [s["odoo_port"], s["code_port"], 22]
            try:
                _authorize_ip(ec2, s["security_group_id"], allow_ip, ports)
                store.update_environment(env_id, allow_ip=allow_ip)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"could not authorize IP {allow_ip}: {exc}")
        run_kwargs = {
            "ImageId": s["ami_id"],
            "InstanceType": s["instance_type"],
            "MinCount": 1,
            "MaxCount": 1,
            "UserData": user_data_bytes,
            "TagSpecifications": [{
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": f"odoo-synth-env-{env_id}"},
                    {"Key": "odoo-synth:env", "Value": env_id},
                    {"Key": "odoo-synth:issue", "Value": issue or ""},
                    {"Key": "odoo-synth:managed", "Value": "true"},
                ],
            }],
        }
        # networking
        net = {"DeviceIndex": 0, "Groups": [s["security_group_id"]],
               "AssociatePublicIpAddress": s["assign_public_ip"]}
        if s.get("subnet_id"):
            net["SubnetId"] = s["subnet_id"]
        run_kwargs["NetworkInterfaces"] = [net]
        if s.get("instance_profile"):
            run_kwargs["IamInstanceProfile"] = {"Name": s["instance_profile"]}
        if s.get("key_name"):
            run_kwargs["KeyName"] = s["key_name"]

        resp = ec2.run_instances(**run_kwargs)
        instance_id = resp["Instances"][0]["InstanceId"]
        store.update_environment(env_id, instance_id=instance_id, status="provisioning")

        # poll for running + public IP
        public_ip = None
        for _ in range(60):
            time.sleep(5)
            d = ec2.describe_instances(InstanceIds=[instance_id])
            inst = d["Reservations"][0]["Instances"][0]
            state = inst["State"]["Name"]
            public_ip = inst.get("PublicIpAddress")
            if state == "running" and public_ip:
                break
            if state in ("terminated", "stopping", "stopped"):
                raise RuntimeError(f"instance entered state {state} during boot")

        if not public_ip and not s["assign_public_ip"]:
            # private-only env: use private IP for the URL
            public_ip = inst.get("PrivateIpAddress")

        vscode_url = f"https://{public_ip}:{s['code_port']}/" if public_ip else None
        odoo_url = f"https://{public_ip}:{s['odoo_port']}/" if public_ip else None
        # Desktop VS Code Remote-SSH deep link (opens the workspace in the local
        # VS Code app, like exe.dev). Works once the user's SSH key can reach the
        # env as ssh_user (inject environments.ssh_public_key + open port 22).
        ssh_user = s.get("ssh_user") or "dev"
        vscode_remote_url = (
            f"vscode://vscode-remote/ssh-remote+{ssh_user}@{public_ip}"
            f"/home/{ssh_user}/workspace?windowId=_blank" if public_ip else None)
        # The instance is up, but Odoo is not yet serving (DB restore + registry
        # load). Record URLs but stay in "booting" until the boot script tags the
        # instance ready (it polls Odoo's /web/health locally). This way the UI
        # only shows "running" when the env is actually reachable.
        store.update_environment(
            env_id, status="booting", public_ip=public_ip,
            vscode_url=vscode_url, odoo_url=odoo_url,
            vscode_remote_url=vscode_remote_url,
        )
        _wait_until_ready(ec2, instance_id, env_id)
    except Exception as exc:  # noqa: BLE001
        store.update_environment(env_id, status="failed", error=str(exc))


def _wait_until_ready(ec2, instance_id: str, env_id: str,
                      timeout_s: int = 900, interval_s: int = 15) -> None:
    """Poll the instance's odoo-synth:ready tag (set by the boot script once Odoo
    answers HTTP) and flip the env to 'running' only then. Falls back to 'running'
    after the timeout so an env is never stuck in 'booting'."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(interval_s)
        # a teardown may have flipped the env; stop polling if so.
        env = store.get_environment(env_id)
        if not env or env.get("status") in ("terminated", "failed"):
            return
        try:
            d = ec2.describe_instances(InstanceIds=[instance_id])
            inst = d["Reservations"][0]["Instances"][0]
            if inst["State"]["Name"] in ("terminated", "stopping", "stopped"):
                store.update_environment(
                    env_id, status="failed",
                    error=f"instance {inst['State']['Name']} while booting")
                return
            tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
            ready = tags.get("odoo-synth:ready")
            if ready == "true":
                store.update_environment(env_id, status="running")
                return
            if ready == "timeout":
                store.update_environment(
                    env_id, status="running",
                    error="odoo slow to answer during boot; may need a moment")
                return
        except Exception:  # noqa: BLE001 — transient AWS errors, keep polling
            continue
    # never leave it stuck in booting
    store.update_environment(env_id, status="running",
                             error="readiness signal not received before timeout")



def create(source_run_id: Optional[str], issue: Optional[str],
           dump_s3_uri: Optional[str], repo_url: Optional[str] = None,
           repo_branch: Optional[str] = None,
           profile_id: Optional[str] = None,
           allow_ip: Optional[str] = None,
           ssh_public_key: Optional[str] = None) -> str:
    env_id = uuid.uuid4().hex[:10]
    store.create_environment(env_id, source_run_id, issue, dump_s3_uri,
                             repo_url=repo_url, repo_branch=repo_branch,
                             profile_id=profile_id)
    import threading
    threading.Thread(
        target=launch,
        args=(env_id, source_run_id, issue, dump_s3_uri, repo_url, repo_branch,
              profile_id, allow_ip, ssh_public_key),
        daemon=True,
    ).start()
    return env_id


def teardown(env_id: str) -> None:
    env = store.get_environment(env_id)
    if not env:
        raise ValueError("environment not found")
    instance_id = env.get("instance_id")
    if instance_id:
        try:
            ec2 = boto3.client("ec2", region_name=_region())
            ec2.terminate_instances(InstanceIds=[instance_id])
        except Exception:  # noqa: BLE001
            pass
    if env.get("secret_arn"):
        _delete_secret(env["secret_arn"])
    allow_ip = env.get("allow_ip")
    if allow_ip:
        s = config.environments_settings()
        _revoke_ip(s["security_group_id"], allow_ip, [s["odoo_port"], s["code_port"], 22])
    store.update_environment(env_id, status="terminated", vscode_url=None)
