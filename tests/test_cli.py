import json

from click.testing import CliRunner

from messnet.cli import cli


def run(tmp_path, *args, input=None, codes=(0,)):
    env = {"MESSNET_NODE": "t", "MESSNET_SPOOL": str(tmp_path / "spool"),
           "MESSNET_LOCAL_SPOOL": str(tmp_path / "local"), "MESSNET_DB": str(tmp_path / "db"),
           "MESSNET_STATE": str(tmp_path / "state"), "MESSNET_CONFIG": "",
           "XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    res = CliRunner().invoke(cli, list(args), env=env, input=input, catch_exceptions=False)
    assert res.exit_code in codes, res.output
    return res.output


def test_help_without_args(tmp_path):
    # Click exits 2 for no_args_is_help commands.
    assert "Usage" in run(tmp_path)
    assert "Usage" in run(tmp_path, "emit", codes=(2,))
    assert "Usage" in run(tmp_path, "tail", codes=(2,))


def test_emit_tail_state(tmp_path):
    run(tmp_path, "emit", "backup.run.v1", "-s", "push", "result=ok", "n:=3")
    run(tmp_path, "emit", "--stdin", input='{"type":"other.v1","data":{"x":1}}\n')
    out = [json.loads(line) for line in run(tmp_path, "tail", "-t", "backup.*").splitlines()]
    assert len(out) == 1 and out[0]["data"] == {"result": "ok", "n": 3}
    assert "other.v1" in run(tmp_path, "tail", "-t", "*", "--format", "text")
    run(tmp_path, "state", "update")
    got = json.loads(run(tmp_path, "state", "get", "backup.*"))
    assert got["subject"] == "push"
    assert run(tmp_path, "vv").strip() == "t/all 2"
