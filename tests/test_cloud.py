"""The ClickHouse Cloud bundles: what build_bundles.py writes, run as ClickHouse Cloud runs it, and the Terraform
module's signatures against the self-hosted config's."""

import json
import re
import stat
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cloud"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "udf"))
import build_bundles
import drex_udf

ROOT = Path(__file__).resolve().parent.parent
KEY = "nace_sk_mockKeyForDrexContractTests_222222222222222"


def test_the_builder_knows_every_function():
    assert set(build_bundles.FUNCTIONS) == set(drex_udf.FUNCTIONS)


def test_each_zip_holds_the_script_its_config_and_requirements(tmp_path):
    paths = build_bundles.build(tmp_path, KEY, "https://console.nace.ai", pool_size=3)
    assert [path.name for path in paths] == [f"drex_{name}.zip" for name in build_bundles.FUNCTIONS]
    for function, path in zip(build_bundles.FUNCTIONS, paths, strict=True):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with zipfile.ZipFile(path) as archive:
            assert sorted(archive.namelist()) == ["config.json", "main.py", "requirements.txt"]
            assert archive.read("main.py") == (ROOT / "udf" / "drex_udf.py").read_bytes()
            assert archive.read("requirements.txt") == (ROOT / "udf" / "requirements.txt").read_bytes()
            assert json.loads(archive.read("config.json")) == {
                "function": function,
                "api_key": KEY,
                "base_url": "https://console.nace.ai",
                "concurrency": 2,
            }


def test_the_same_inputs_build_the_same_zip(tmp_path):
    first = build_bundles.build(tmp_path / "a", KEY, "https://console.nace.ai", pool_size=3)
    second = build_bundles.build(tmp_path / "b", KEY, "https://console.nace.ai", pool_size=3)
    assert [path.read_bytes() for path in first] == [path.read_bytes() for path in second]


@pytest.mark.parametrize(("pool_size", "concurrency"), [(1, 4), (2, 4), (3, 2), (8, 1), (16, 1)])
def test_concurrency_shares_8_in_flight_across_the_pool_at_most_4_a_process(tmp_path, pool_size, concurrency):
    (path, *_) = build_bundles.build(tmp_path, KEY, "https://console.nace.ai", pool_size=pool_size)
    with zipfile.ZipFile(path) as archive:
        assert json.loads(archive.read("config.json"))["concurrency"] == concurrency


def test_the_base_url_must_be_https(tmp_path):
    with pytest.raises(ValueError, match="https://"):
        build_bundles.build(tmp_path, KEY, "http://console.nace.ai", pool_size=3)


def test_the_attribution_is_written_to_config_json_only_when_set(tmp_path):
    url = "https://openrouter.ai/api"
    (path, *_) = build_bundles.build(tmp_path / "a", "sk-or-v1-key", url, pool_size=3)
    with zipfile.ZipFile(path) as archive:
        assert "openrouter_referer" not in json.loads(archive.read("config.json"))
    (path, *_) = build_bundles.build(tmp_path / "b", "sk-or-v1-key", url, 3, "https://app.example", "Example")
    with zipfile.ZipFile(path) as archive:
        config = json.loads(archive.read("config.json"))
    assert (config["openrouter_referer"], config["openrouter_title"]) == ("https://app.example", "Example")


def test_a_nace_key_is_not_bundled_for_open_router(tmp_path):
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        build_bundles.build(tmp_path, KEY, "https://openrouter.ai/api", pool_size=3)


def test_the_builder_prefers_openrouter_api_key_for_open_router(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "NACE_BASE_URL": "https://openrouter.ai/api", "NACE_API_KEY": KEY}
    env["OPENROUTER_API_KEY"] = "sk-or-v1-fromOpenRouterVar"
    result = subprocess.run(
        [sys.executable, str(ROOT / "cloud" / "build_bundles.py"), "--output", str(tmp_path)],
        env=env,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(tmp_path / "drex_noul.zip") as archive:
        assert json.loads(archive.read("config.json"))["api_key"] == "sk-or-v1-fromOpenRouterVar"


def test_the_key_comes_from_the_environment(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "cloud" / "build_bundles.py"), "--output", str(tmp_path)],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
    )
    assert result.returncode == 2
    assert b"set NACE_API_KEY" in result.stderr


def xml_signatures():
    root = ElementTree.parse(ROOT / "config" / "drex_function.xml").getroot()
    return {
        function.find("name").text: (
            function.find("return_type").text,
            [(argument.find("name").text, argument.find("type").text) for argument in function.iter("argument")],
        )
        for function in root.iter("function")
    }


def test_terraform_declares_the_self_hosted_signatures():
    terraform = (ROOT / "cloud" / "terraform" / "main.tf").read_text()
    for name, (return_type, arguments) in xml_signatures().items():
        block = re.search(rf"\n    {name.removeprefix('drex_')} = {{\n(.*?)\n    }}\n", terraform, re.DOTALL)
        assert block, name
        assert f'return_type = "{return_type}"' in block.group(1)
        assert re.findall(r'\{ name = "(\w+)", type = "([^"]+)" \}', block.group(1)) == arguments


def test_terraform_matches_the_self_hosted_settings():
    terraform = (ROOT / "cloud" / "terraform" / "main.tf").read_text()
    shipped = ElementTree.parse(ROOT / "config" / "drex_function.xml").getroot().find("function")
    assert 'format            = "RowBinary"' in terraform and shipped.find("format").text == "RowBinary"
    assert "send_chunk_header = true" in terraform and shipped.find("send_chunk_header").text == "1"
    for setting in ("command_read_timeout", "max_command_execution_time"):
        assert f"{setting} = {shipped.find(setting).text}" in re.sub(r" +=", " =", terraform)
