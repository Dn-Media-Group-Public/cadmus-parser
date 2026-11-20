"""Job-scoped store credentials for a consumer of the wrapper: `mint(grants)` turns "these URLs, read or write" into the fsspec `storage_options`
fragment each URL needs, limited to exactly those prefixes. `read` means read and list; `write` means read, list and write (the wrapper reads
status.json under the output base and lists the input base).

S3 / SeaweedFS: one IAM role per store with its own policy (no broad role behind it, no session policy to forget), assumed with STS; deleting the
role revokes the credential at once. GCS: a Credential Access Boundary (a downscoped token): narrow by construction, it cannot be revoked and
lives as long as the source credential. Every failure raises: there is no fallback to the consumer's own credential.

`mint` returns a `Handle`: the `{url: options}` mapping, with `revoke()`, a persistable form that holds only what is needed to revoke (never a
secret) and a context manager. `sweep(max_age)` removes helper-created roles that were never revoked. `options` is the consumer's own identity
for the store (fsspec-style: key, secret, token, endpoint_url, client_kwargs); without it boto3 / google-auth find their defaults."""

import collections.abc
import json
import logging
import secrets
import time
from urllib.parse import urlparse

log = logging.getLogger("cadmus_parser.minting")
ROLE_PREFIX = "cadmus-parser-"  # the helper's own roles: sweep touches only these
LIFETIME = 900  # seconds: the minimum for SeaweedFS and AWS STS
S3_SCHEMES, GCS_SCHEMES = ("s3", "s3a"), ("gs", "gcs")


def _split(url: str) -> tuple[str, str, str]:
    u = urlparse(url)
    return u.scheme, u.netloc, u.path.strip("/")  # scheme, bucket, key prefix inside it ("" = none)


def s3_policy(bucket: str, grants: list[tuple[str, str]]) -> dict:
    """The role's policy for `grants` = [(key prefix, "read" | "write")] in one bucket: read (get, list) everywhere a grant reaches, write (put,
    delete, multipart) only where it says write. s3: actions only."""
    def res(p):
        return [f"arn:aws:s3:::{bucket}/{p}", f"arn:aws:s3:::{bucket}/{p}/*"] if p else [f"arn:aws:s3:::{bucket}/*"]
    reads = sorted({r for p, _ in grants for r in res(p)})
    writes = sorted({r for p, m in grants if m == "write" for r in res(p)})
    prefixes = sorted({q for p, _ in grants for q in ((p, f"{p}/*") if p else ("", "*"))})
    st = [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": reads}]
    if writes:
        st.append({"Effect": "Allow", "Action": ["s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"], "Resource": writes})
    st.append({"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}", "Condition": {"StringLike": {"s3:prefix": prefixes}}})
    return {"Version": "2012-10-17", "Statement": st}


def cab_rules(bucket: str, grants: list[tuple[str, str]]) -> list[dict]:
    """GCS Credential Access Boundary rules (at most 10 per boundary; two here): read under every grant (and list it), write under the write grants."""
    if any("'" in p for p, _ in grants):
        raise ValueError("a quote in a key prefix")
    res, obj = f"//storage.googleapis.com/projects/_/buckets/{bucket}", f"projects/_/buckets/{bucket}/objects/"
    listing = "api.getAttribute('storage.googleapis.com/objectListPrefix', '')"

    def names(ps):
        return " || ".join(f"resource.name.startsWith('{obj}{p}/') || resource.name == '{obj}{p}'" if p else f"resource.name.startsWith('{obj}')" for p in ps)
    ps = sorted({p for p, _ in grants})
    lists = " || ".join(f"{listing}.startsWith('{p}/') || {listing} == '{p}'" if p else f"{listing}.startsWith('')" for p in ps)
    rules = [{"resource": res, "permissions": ["inRole:roles/storage.objectViewer"], "condition": f"{names(ps)} || {lists}"}]
    if ws := sorted({p for p, m in grants if m == "write"}):
        rules.append({"resource": res, "permissions": ["inRole:roles/storage.objectUser"], "condition": names(ws)})
    return rules


def _aws(service: str, options: dict):
    import boto3  # optional extra: cadmus-parser[mint-s3]
    ck = options.get("client_kwargs") or {}
    return boto3.client(service, endpoint_url=options.get("endpoint_url") or ck.get("endpoint_url"), region_name=ck.get("region_name") or "us-east-1",
                        aws_access_key_id=options.get("key"), aws_secret_access_key=options.get("secret"), aws_session_token=options.get("token"))


def _delete_role(iam, name: str) -> None:
    try:
        for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
            iam.delete_policy(PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=name)
    except Exception as e:  # noqa: BLE001
        if "NoSuchEntity" not in f"{type(e).__name__} {e}":  # already gone is fine: revoke is idempotent
            raise


class Handle(collections.abc.Mapping):
    """The `{url: options}` mapping `mint` made, plus what it takes to end the credentials. A handle restored with `from_dict` can `revoke()`,
    but its credentials exist only in the process that minted them (it is an empty mapping)."""

    def __init__(self, creds: dict, stores: list[dict], options: dict | None = None):
        self._creds, self.stores, self._options, self.revoked = creds, stores, options or {}, False

    def __getitem__(self, url):
        return self._creds[url]

    def __iter__(self):
        return iter(self._creds)

    def __len__(self):
        return len(self._creds)

    def revoke(self) -> None:
        """End the credentials where the store allows it (S3: the role is deleted; GCS: nothing, the token expires). Idempotent."""
        if self.revoked:
            return
        for s in self.stores:
            if s["kind"] == "s3":
                _delete_role(_aws("iam", {**self._options, "endpoint_url": s.get("endpoint_url") or self._options.get("endpoint_url")}), s["role"])
        self.revoked = True

    def to_dict(self) -> dict:
        """A plain structure for a database or a log: role identifiers, the store and the creation time. No secret."""
        return {"version": 1, "stores": self.stores}

    @classmethod
    def from_dict(cls, d: dict, options: dict | None = None) -> "Handle":
        """Restore for `revoke()`; `options` is the consumer's own identity (not persisted: it is a secret)."""
        return cls({}, list(d["stores"]), options)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.revoke()


def _mint_s3(bucket: str, grants: list, lifetime: int, options: dict) -> tuple[dict, dict]:
    iam, sts = _aws("iam", options), _aws("sts", options)
    name = f"{ROLE_PREFIX}{secrets.token_hex(12)}"
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                     "Principal": {"AWS": sts.get_caller_identity()["Arn"]}}]}  # the consumer's own identity
    role = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
    try:
        pol = iam.create_policy(PolicyName=name, PolicyDocument=json.dumps(s3_policy(bucket, grants), separators=(",", ":")))["Policy"]["Arn"]
        iam.attach_role_policy(RoleName=name, PolicyArn=pol)
        c = sts.assume_role(RoleArn=role, RoleSessionName=name[:64], DurationSeconds=lifetime)["Credentials"]
    except BaseException:
        _delete_role(iam, name)
        raise
    return ({"key": c["AccessKeyId"], "secret": c["SecretAccessKey"], "token": c["SessionToken"]},
            {"kind": "s3", "bucket": bucket, "role": name, "created": time.time(), "endpoint_url": options.get("endpoint_url") or (options.get("client_kwargs") or {}).get("endpoint_url")})


def _mint_gcs(bucket: str, grants: list) -> tuple[dict, dict]:
    import google.auth  # optional extra: cadmus-parser[mint-gcs]
    from google.auth import downscoped
    src, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    rules = [downscoped.AccessBoundaryRule(available_resource=r["resource"], available_permissions=r["permissions"],
                                           availability_condition=downscoped.AvailabilityCondition(r["condition"])) for r in cab_rules(bucket, grants)]
    # ponytail: the token lives as long as the source credential (not settable) and cannot be revoked
    return {"token": downscoped.Credentials(src, downscoped.CredentialAccessBoundary(rules))}, {"kind": "gcs", "bucket": bucket, "created": time.time()}


def mint(grants: list[tuple[str, str]], lifetime: int = LIFETIME, options: dict | None = None) -> Handle:
    """`grants` = [(url, "read" | "write")]. One credential (one role, policy and session) per store; grants in the same store share it and their
    entries in the mapping hold the same credential. Raises ValueError for a bad grant and RuntimeError for a scheme without a minting mechanism."""
    options = options or {}
    groups: dict = {}
    for url, mode in grants:
        if mode not in ("read", "write"):
            raise ValueError(f"grant mode must be read or write, not {mode!r}")
        scheme, bucket, prefix = _split(url)
        kind = "s3" if scheme in S3_SCHEMES else "gcs" if scheme in GCS_SCHEMES else None
        if kind is None:
            raise RuntimeError(f"no scoped credentials for {url!r}")
        groups.setdefault((kind, bucket), []).append((url, prefix, mode))
    creds, stores = {}, []
    h = Handle(creds, stores, options)
    try:
        for (kind, bucket), gs in groups.items():
            pg = [(p, m) for _, p, m in gs]
            frag, info = _mint_s3(bucket, pg, lifetime, options) if kind == "s3" else _mint_gcs(bucket, pg)
            stores.append(info)
            creds.update({url: frag for url, _, _ in gs})
    except BaseException:
        h.revoke()  # no half-minted set: the roles of the stores that did succeed go
        raise
    return h


def sweep(max_age: float = LIFETIME + 300, options: dict | None = None) -> int:
    """Delete helper-created roles (prefix `cadmus-parser-`) older than `max_age` seconds: those a crashed consumer never revoked. S3 only; the
    consumer schedules it. Returns the number deleted."""
    iam, n = _aws("iam", options or {}), 0
    for page in iam.get_paginator("list_roles").paginate():
        for r in page["Roles"]:
            if r["RoleName"].startswith(ROLE_PREFIX) and time.time() - r["CreateDate"].timestamp() > max_age:
                try:
                    _delete_role(iam, r["RoleName"])
                    n += 1
                except Exception:  # noqa: BLE001
                    log.exception("sweep: could not delete %s", r["RoleName"])
    return n
