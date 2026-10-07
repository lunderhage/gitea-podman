"""Safe YAML settings, with read/write compatibility for existing JSON files."""
import json
from pathlib import Path
import re
import sys
import yaml


class SettingsLoader(yaml.SafeLoader):
    pass


def mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("Settings keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


SettingsLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def settings_path(root):
    root = Path(root)
    yaml_files = [root / name for name in ("settings.yaml", "settings.yml") if (root / name).exists()]
    if len(yaml_files) > 1:
        raise ValueError("Keep only one YAML settings file: settings.yaml or settings.yml")
    if yaml_files:
        return yaml_files[0]
    legacy = root / "settings.json"
    return legacy if legacy.exists() else root / "settings.yaml"


def load_settings(filename):
    filename = Path(filename)
    text = filename.read_text(encoding="utf-8")
    result = json.loads(text) if filename.suffix == ".json" else yaml.load(text, Loader=SettingsLoader)
    if not isinstance(result, dict):
        raise ValueError("Settings must be a mapping")
    return result


def save_settings(filename, values):
    filename = Path(filename)
    text = (json.dumps(values, indent=2) + "\n" if filename.suffix == ".json"
            else yaml.safe_dump(values, sort_keys=False))
    temporary = filename.with_name(filename.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(filename)


def compose_values(values):
    patterns = {"project": r"[a-zA-Z0-9_-]+", "dataVolume": r"[a-zA-Z0-9][a-zA-Z0-9_.-]*",
                "configVolume": r"[a-zA-Z0-9][a-zA-Z0-9_.-]*",
                "image": r"docker\.gitea\.com/gitea(?::[0-9]+\.[0-9]+\.[0-9]+-rootless|@sha256:[a-f0-9]{64})"}
    for key, pattern in patterns.items():
        if not isinstance(values.get(key), str) or not re.fullmatch(pattern, values[key]):
            raise ValueError("Invalid Compose setting: " + key)
    if values["dataVolume"] == values["configVolume"]:
        raise ValueError("Data and config volumes must differ")
    for key in ("httpPort", "sshPort"):
        if type(values.get(key)) is not int or not 1024 <= values[key] <= 65535:
            raise ValueError("Invalid Compose setting: " + key)
    backup = values.get("backupVolume", values["project"] + "-backups")
    domain = values.get("sshDomain", "localhost")
    listen = values.get("sshListenPort", 2222)
    if not isinstance(backup, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", backup):
        raise ValueError("Invalid backup volume")
    if not isinstance(domain, str) or not re.fullmatch(r"[a-zA-Z0-9._:-]+", domain):
        raise ValueError("Invalid SSH domain")
    if type(listen) is not int or not 1024 <= listen <= 65535:
        raise ValueError("Invalid SSH listen port")
    return (values["image"], values["dataVolume"], values["configVolume"], values["project"],
            values["httpPort"], values["sshPort"], backup, domain, listen)


if __name__ == "__main__":
    try:
        if sys.argv[1] == "exports":
            for value in compose_values(load_settings(settings_path(sys.argv[2]))):
                print(value)
        elif sys.argv[1] == "migrate":
            root = Path(sys.argv[2])
            destination = root / "settings.yaml"
            if destination.exists() or (root / "settings.yml").exists():
                raise ValueError("YAML settings already exist; refusing to overwrite")
            save_settings(destination, load_settings(root / "settings.json"))
            print(f"Created {destination}; original JSON file preserved. YAML now takes precedence.")
        else:
            raise ValueError("Unknown settings command")
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
