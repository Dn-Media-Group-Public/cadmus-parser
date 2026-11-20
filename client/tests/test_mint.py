"""The minting helper without a cloud: the policies, and the role lifecycle against fake IAM/STS clients."""

import datetime
import json

import pytest

from cadmus_parser import minting as m


class FakeIAM:
    def __init__(self):
        self.roles, self.policies, self.attached, self.calls = {}, {}, {}, []

    def create_role(self, RoleName, AssumeRolePolicyDocument):
        self.roles[RoleName] = datetime.datetime.now(datetime.timezone.utc)
        return {"Role": {"Arn": f"arn:role/{RoleName}"}}

    def create_policy(self, PolicyName, PolicyDocument):
        self.policies[PolicyName] = json.loads(PolicyDocument)
        return {"Policy": {"Arn": f"arn:policy/{PolicyName}"}}

    def attach_role_policy(self, RoleName, PolicyArn):
        self.attached[RoleName] = PolicyArn

    def list_attached_role_policies(self, RoleName):
        if RoleName not in self.roles:
            raise RuntimeError("NoSuchEntity")
        return {"AttachedPolicies": [{"PolicyArn": self.attached[RoleName]}] if RoleName in self.attached else []}

    def detach_role_policy(self, RoleName, PolicyArn):
        self.attached.pop(RoleName)

    def delete_policy(self, PolicyArn):
        self.policies.pop(PolicyArn.rsplit("/", 1)[1])

    def delete_role(self, RoleName):
        self.roles.pop(RoleName)

    def get_paginator(self, name):
        iam = self
        return type("P", (), {"paginate": lambda s: [{"Roles": [{"RoleName": n, "CreateDate": d} for n, d in iam.roles.items()]}]})()


class FakeSTS:
    def get_caller_identity(self):
        return {"Arn": "arn:me"}

    def assume_role(self, RoleArn, RoleSessionName, DurationSeconds):
        return {"Credentials": {"AccessKeyId": f"AK-{RoleArn}", "SecretAccessKey": "sk", "SessionToken": f"tok-{DurationSeconds}"}}


@pytest.fixture
def aws(monkeypatch):
    iam, sts = FakeIAM(), FakeSTS()
    monkeypatch.setattr(m, "_aws", lambda service, options: iam if service == "iam" else sts)
    return iam


def test_s3_policy_read_is_get_and_list_write_adds_put_and_delete_only_where_granted():
    p = m.s3_policy("bkt", [("pre/in", "read"), ("pre/out", "write")])
    get, put, ls = p["Statement"]
    assert get["Resource"] == ["arn:aws:s3:::bkt/pre/in", "arn:aws:s3:::bkt/pre/in/*", "arn:aws:s3:::bkt/pre/out", "arn:aws:s3:::bkt/pre/out/*"]  # write includes read
    assert put["Resource"] == ["arn:aws:s3:::bkt/pre/out", "arn:aws:s3:::bkt/pre/out/*"] and "s3:GetObject" not in put["Action"]
    assert ls["Action"] == "s3:ListBucket" and ls["Resource"] == "arn:aws:s3:::bkt"
    assert ls["Condition"]["StringLike"]["s3:prefix"] == ["pre/in", "pre/in/*", "pre/out", "pre/out/*"]
    assert all(a.startswith("s3:") for st in p["Statement"] for a in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"]))
    assert [st["Action"] for st in m.s3_policy("bkt", [("a", "read")])["Statement"]] == ["s3:GetObject", "s3:ListBucket"]  # no write statement at all
    assert m.s3_policy("bkt", [("", "read")])["Statement"][0]["Resource"] == ["arn:aws:s3:::bkt/*"]  # a whole bucket, when that is the grant


def test_cab_rules():
    r = m.cab_rules("bkt", [("pre/in", "read"), ("pre/out", "write")])
    assert len(r) == 2 and "objectViewer" in r[0]["permissions"][0] and "objectUser" in r[1]["permissions"][0]
    assert "objects/pre/out/'" in r[1]["condition"] and "pre/in" not in r[1]["condition"] and "objectListPrefix" in r[0]["condition"]
    assert len(m.cab_rules("bkt", [("a", "read")])) == 1
    with pytest.raises(ValueError):
        m.cab_rules("bkt", [("a'b", "read")])


def test_one_role_per_store_and_the_mapping_is_keyed_by_url(aws):
    h = m.mint([("s3://bkt/p/in", "read"), ("s3://bkt/p/out", "write"), ("s3://other/x", "write")], lifetime=1200)
    assert set(h) == {"s3://bkt/p/in", "s3://bkt/p/out", "s3://other/x"} and len(aws.roles) == 2 and len(h.stores) == 2
    assert h["s3://bkt/p/in"] is h["s3://bkt/p/out"] and h["s3://other/x"] != h["s3://bkt/p/out"]  # grants in one store share one credential
    assert h["s3://bkt/p/in"]["token"] == "tok-1200" and set(h["s3://bkt/p/in"]) == {"key", "secret", "token"}
    assert all(r.startswith(m.ROLE_PREFIX) for r in aws.roles)
    h.revoke()
    h.revoke()  # idempotent
    assert aws.roles == {} and aws.policies == {} and aws.attached == {}


def test_context_manager_revokes(aws):
    with m.mint([("s3://bkt/p", "write")]) as h:
        assert len(aws.roles) == 1 and "s3://bkt/p" in h
    assert aws.roles == {}
    with pytest.raises(KeyError):
        with m.mint([("s3://bkt/p", "write")]):
            assert len(aws.roles) == 1
            raise KeyError("x")
    assert aws.roles == {}


def test_persisted_handle_holds_no_secret_and_revokes_in_another_process(aws):
    h = m.mint([("s3://bkt/p", "write")])
    d = json.loads(json.dumps(h.to_dict()))  # plain structure: a database column
    assert "sk" not in json.dumps(d) and "tok-" not in json.dumps(d) and set(d["stores"][0]) >= {"kind", "bucket", "role", "created"}
    again = m.Handle.from_dict(d)
    assert len(again) == 0
    again.revoke()
    assert aws.roles == {}
    h.revoke()  # the original finds the role gone: no error


def test_failure_leaves_no_role_behind(aws, monkeypatch):
    class Boom(FakeSTS):
        def assume_role(self, **kw):
            raise RuntimeError("denied")
    monkeypatch.setattr(m, "_aws", lambda service, options: aws if service == "iam" else Boom())
    with pytest.raises(RuntimeError, match="denied"):
        m.mint([("s3://bkt/p", "read")])
    assert aws.roles == {} and aws.policies == {}
    with pytest.raises(RuntimeError, match="no scoped credentials"):
        m.mint([("file:///tmp/x", "read")])
    with pytest.raises(ValueError):
        m.mint([("s3://bkt/p", "admin")])


def test_a_failing_second_store_revokes_the_first(aws, monkeypatch):
    real, n = m._mint_s3, []

    def flaky(bucket, grants, lifetime, options):
        n.append(bucket)
        if len(n) == 2:
            raise RuntimeError("second store down")
        return real(bucket, grants, lifetime, options)
    monkeypatch.setattr(m, "_mint_s3", flaky)
    with pytest.raises(RuntimeError, match="second"):
        m.mint([("s3://a/x", "read"), ("s3://b/y", "read")])
    assert aws.roles == {}


def test_sweep_removes_only_old_helper_roles(aws):
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)
    aws.roles.update({f"{m.ROLE_PREFIX}old": old, "someone-elses-role": old})
    fresh = m.mint([("s3://bkt/p", "read")])
    assert m.sweep(max_age=3600) == 1
    assert set(aws.roles) == {"someone-elses-role", fresh.stores[0]["role"]}
