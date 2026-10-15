"""Reuse the adapter's fake server through the same injectable connect boundary."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from anyship_adapters.compose_host import ComposeHost
from anyship_adapters.deployer import Deployer
from anyship_adapters.image_builder import BuiltImage
from anyship_adapters.onprem import OnpremAdapter
from anyship_adapters.ssh import SshConnection, SshRunner
from app.onprem_transport import Transport

ROOT = Path(__file__).resolve().parents[2]


def load_fixture(name):
    spec = importlib.util.spec_from_file_location(
        "onprem_fixture_" + name, ROOT / "infra/adapters/tests" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Builder:
    def build(self, source_dir, app, sha, log, **kwargs):
        return BuiltImage(image=f"{app}:{sha}", image_id="sha256:fake")

    def prune(self, app, keep):
        return []


class DNS:
    def __init__(self):
        self.records = {}

    def name_for(self, env_id):
        return f"*.{env_id}.onprem.anyship.cloud"

    def ensure(self, env_id, ip):
        self.records[env_id] = ip
        return True

    def remove(self, env_id):
        self.records.pop(env_id, None)
        return True


class RecordingTransport(Transport):
    def __init__(self, directory):
        key = directory / "fixture_key"
        Path(str(key) + ".pub").write_text(
            "ssh-ed25519 AAAA" + "A" * 64 + " fixture", encoding="utf-8"
        )
        super().__init__(SimpleNamespace(deploy_ssh_key=key, app_origin="http://localhost:8000"))
        self.tokens = {}

    def command(self, row, token):
        self.tokens[row.id] = token
        return super().command(row, token)


def parts():
    server = load_fixture("fakes").LifecycleServer()
    dns = DNS()
    runner = SshRunner(SshConnection("3.38.88.141", Path("fixture-key")), runner=server)
    host = ComposeHost(runner, popen=server.popen)
    adapter = OnpremAdapter(
        Path("fixture-key"),
        connect=lambda env: (runner, host),
        healthy=lambda *args, **kwargs: (True, 200),
        dns=dns,
    )
    return SimpleNamespace(
        server=server, dns=dns, deployer=Deployer({"onprem": adapter}, Builder())
    )
