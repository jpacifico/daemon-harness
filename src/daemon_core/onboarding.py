import argparse
import getpass
import re
import shlex
import shutil
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values

from daemon_core.paths import PROJECT_ROOT

CRON_MARKER = "# Daemon Harness dreaming daily"

SECRET_KEYS = {
    "OPENAI_API_KEY",
    "LOCAL_LLM_API_KEY",
    "TELEGRAM_BOT_TOKEN",
}

DEFAULTS = {
    "LLM_PROVIDER": "openai",
    "OPENAI_MODEL": "gpt-5.6-terra",
    "ROUTING_OPENAI_MODEL": "gpt-5.6-terra",
    "CONVERSATION_OPENAI_MODEL": "gpt-5.6-terra",
    "MEMORY_OPENAI_MODEL": "gpt-5.6-terra",
    "LOCAL_LLM_BASE_URL": "http://localhost:1234/v1",
    "LOCAL_LLM_API_KEY": "local-key",
    "LOCAL_LLM_MODEL": "local-model-name",
    "CODEX_CLI_BIN": "codex",
    "CODEX_CLI_MODEL": "gpt-5.6-terra",
    "CODEX_CLI_TIMEOUT_SECONDS": "300",
    "MEMORY_CODEX_MODEL": "gpt-5.6-terra",
    "DREAMING_SINCE_HOURS": "24",
    "DREAMING_DAILY_TIME": "04:00",
}

ORDERED_KEYS = [
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "ROUTING_LLM_PROVIDER",
    "ROUTING_OPENAI_MODEL",
    "CONVERSATION_LLM_PROVIDER",
    "CONVERSATION_OPENAI_MODEL",
    "MEMORY_LLM_PROVIDER",
    "MEMORY_OPENAI_MODEL",
    "LOCAL_LLM_BASE_URL",
    "LOCAL_LLM_API_KEY",
    "LOCAL_LLM_MODEL",
    "ROUTING_LOCAL_LLM_MODEL",
    "CONVERSATION_LOCAL_LLM_MODEL",
    "MEMORY_LOCAL_LLM_MODEL",
    "CODEX_CLI_BIN",
    "CODEX_CLI_MODEL",
    "CODEX_CLI_PROFILE",
    "CODEX_CLI_TIMEOUT_SECONDS",
    "MEMORY_CODEX_MODEL",
    "TELEGRAM_BOT_TOKEN",
    "DAEMON_CORE_USER_ID",
    "DAEMON_CORE_VOICE_CAPTURE_FILE",
    "DREAMING_SINCE_HOURS",
    "DREAMING_DAILY_TIME",
]


def run_onboarding(env_path: Path | None = None) -> None:
    env_path = env_path or PROJECT_ROOT / ".env"
    ensure_env_file(env_path)
    current = load_env(env_path)

    print("Daemon Harness onboarding")
    print("Appuie sur Entrée pour conserver la valeur actuelle entre crochets.")
    print("Les secrets saisis ne sont pas affichés.\n")

    updates: dict[str, str] = {}
    provider = ask_choice(
        "Provider principal",
        choices=["openai", "local", "echo"],
        current=current.get("LLM_PROVIDER") or DEFAULTS["LLM_PROVIDER"],
    )
    updates["LLM_PROVIDER"] = provider

    if provider == "openai":
        updates["OPENAI_API_KEY"] = ask_secret("OPENAI_API_KEY", current.get("OPENAI_API_KEY", ""))
        updates["OPENAI_MODEL"] = ask_text("OPENAI_MODEL", current.get("OPENAI_MODEL") or DEFAULTS["OPENAI_MODEL"])
        updates["ROUTING_LLM_PROVIDER"] = ask_choice("ROUTING_LLM_PROVIDER", ["", "openai", "local", "echo"], current.get("ROUTING_LLM_PROVIDER", "openai"))
        updates["ROUTING_OPENAI_MODEL"] = ask_text("ROUTING_OPENAI_MODEL", current.get("ROUTING_OPENAI_MODEL") or updates["OPENAI_MODEL"])
        updates["CONVERSATION_LLM_PROVIDER"] = ask_choice("CONVERSATION_LLM_PROVIDER", ["", "openai", "local", "echo"], current.get("CONVERSATION_LLM_PROVIDER", "openai"))
        updates["CONVERSATION_OPENAI_MODEL"] = ask_text("CONVERSATION_OPENAI_MODEL", current.get("CONVERSATION_OPENAI_MODEL") or updates["OPENAI_MODEL"])
    elif provider == "local":
        updates["LOCAL_LLM_BASE_URL"] = ask_text("LOCAL_LLM_BASE_URL", current.get("LOCAL_LLM_BASE_URL") or DEFAULTS["LOCAL_LLM_BASE_URL"])
        updates["LOCAL_LLM_API_KEY"] = ask_secret("LOCAL_LLM_API_KEY", current.get("LOCAL_LLM_API_KEY") or DEFAULTS["LOCAL_LLM_API_KEY"])
        updates["LOCAL_LLM_MODEL"] = ask_text("LOCAL_LLM_MODEL", current.get("LOCAL_LLM_MODEL") or DEFAULTS["LOCAL_LLM_MODEL"])
        updates["ROUTING_LLM_PROVIDER"] = ask_choice("ROUTING_LLM_PROVIDER", ["", "openai", "local", "echo"], current.get("ROUTING_LLM_PROVIDER", "local"))
        updates["CONVERSATION_LLM_PROVIDER"] = ask_choice("CONVERSATION_LLM_PROVIDER", ["", "openai", "local", "echo"], current.get("CONVERSATION_LLM_PROVIDER", "local"))
    else:
        updates["ROUTING_LLM_PROVIDER"] = ask_choice("ROUTING_LLM_PROVIDER", ["", "openai", "local", "echo"], current.get("ROUTING_LLM_PROVIDER", ""))
        updates["CONVERSATION_LLM_PROVIDER"] = ask_choice("CONVERSATION_LLM_PROVIDER", ["", "openai", "local", "echo"], current.get("CONVERSATION_LLM_PROVIDER", ""))

    print("\nMémoire et dreaming")
    memory_provider = ask_choice(
        "MEMORY_LLM_PROVIDER",
        choices=["", "openai", "local", "codex_cli", "echo"],
        current=current.get("MEMORY_LLM_PROVIDER", "codex_cli" if provider == "openai" else provider),
    )
    updates["MEMORY_LLM_PROVIDER"] = memory_provider
    if memory_provider == "openai":
        updates["OPENAI_API_KEY"] = updates.get("OPENAI_API_KEY") or ask_secret("OPENAI_API_KEY", current.get("OPENAI_API_KEY", ""))
        updates["MEMORY_OPENAI_MODEL"] = ask_text("MEMORY_OPENAI_MODEL", current.get("MEMORY_OPENAI_MODEL") or DEFAULTS["MEMORY_OPENAI_MODEL"])
    elif memory_provider == "local":
        updates["LOCAL_LLM_BASE_URL"] = updates.get("LOCAL_LLM_BASE_URL") or ask_text("LOCAL_LLM_BASE_URL", current.get("LOCAL_LLM_BASE_URL") or DEFAULTS["LOCAL_LLM_BASE_URL"])
        updates["MEMORY_LOCAL_LLM_MODEL"] = ask_text("MEMORY_LOCAL_LLM_MODEL", current.get("MEMORY_LOCAL_LLM_MODEL") or current.get("LOCAL_LLM_MODEL") or DEFAULTS["LOCAL_LLM_MODEL"])
    elif memory_provider == "codex_cli":
        updates["CODEX_CLI_BIN"] = ask_text("CODEX_CLI_BIN", current.get("CODEX_CLI_BIN") or DEFAULTS["CODEX_CLI_BIN"])
        updates["MEMORY_CODEX_MODEL"] = ask_text("MEMORY_CODEX_MODEL", current.get("MEMORY_CODEX_MODEL") or DEFAULTS["MEMORY_CODEX_MODEL"])
        updates["CODEX_CLI_TIMEOUT_SECONDS"] = ask_text("CODEX_CLI_TIMEOUT_SECONDS", current.get("CODEX_CLI_TIMEOUT_SECONDS") or DEFAULTS["CODEX_CLI_TIMEOUT_SECONDS"])

    print("\nTelegram et automation")
    updates["TELEGRAM_BOT_TOKEN"] = ask_optional_secret("TELEGRAM_BOT_TOKEN", current.get("TELEGRAM_BOT_TOKEN", ""))
    updates["DAEMON_CORE_USER_ID"] = ask_text("DAEMON_CORE_USER_ID", current.get("DAEMON_CORE_USER_ID", ""))
    updates["DREAMING_SINCE_HOURS"] = ask_text("DREAMING_SINCE_HOURS", current.get("DREAMING_SINCE_HOURS") or DEFAULTS["DREAMING_SINCE_HOURS"])
    updates["DREAMING_DAILY_TIME"] = ask_daily_time("DREAMING_DAILY_TIME", current.get("DREAMING_DAILY_TIME") or DEFAULTS["DREAMING_DAILY_TIME"])

    write_env_updates(env_path, updates)
    print("\n.env mis à jour.")
    print_summary(updates)
    if updates.get("DAEMON_CORE_USER_ID"):
        seeded = seed_conversation_manner(updates["DAEMON_CORE_USER_ID"])
        if seeded:
            print(f"Manière de converser par défaut installée : {seeded}")
    if ask_yes_no("\nInstaller ou mettre à jour le cron dreaming quotidien ?", current=True):
        if not updates["DAEMON_CORE_USER_ID"]:
            print("Cron ignoré : DAEMON_CORE_USER_ID est vide.")
        else:
            cron_line = build_dreaming_cron_line(
                user_id=updates["DAEMON_CORE_USER_ID"],
                since_hours=updates["DREAMING_SINCE_HOURS"],
                daily_time=updates["DREAMING_DAILY_TIME"],
                project_root=PROJECT_ROOT,
            )
            install_dreaming_cron(cron_line)
            print(f"Cron dreaming installé : {cron_line}")
    if updates.get("TELEGRAM_BOT_TOKEN") and ask_yes_no("\nInstaller ou mettre à jour le service Telegram permanent ?", current=False):
        try:
            install_telegram_service(project_root=PROJECT_ROOT)
        except Exception as exc:
            print(f"Installation du service Telegram annulée : {exc}")
    if ask_yes_no("\nImporter un bundle mémoire maintenant ?", current=False):
        bundle_path = ask_text("Chemin du bundle mémoire .tar/.tar.gz/.tgz", "")
        if bundle_path:
            try:
                backup_path = import_memory_bundle(Path(bundle_path), project_root=PROJECT_ROOT)
            except Exception as exc:
                print(f"Import mémoire annulé : {exc}")
            else:
                print(f"Bundle mémoire importé. Backup précédent : {backup_path}")


def seed_conversation_manner(user_id: str, project_root: Path = PROJECT_ROOT) -> Path | None:
    """Initialize a new user's default conversational style.

    Never overwrite an existing file: the style belongs to the user."""
    template = project_root / "memory" / "templates" / "users" / "conversation.md"
    if not template.is_file():
        return None
    from daemon_core.memory.writer import safe_memory_user_id

    target = project_root / "memory" / "users" / safe_memory_user_id(user_id) / "conversation.md"
    if target.exists():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, target)
    return target


def ensure_env_file(env_path: Path) -> None:
    if env_path.exists():
        return
    example = env_path.with_name(".env.example")
    if example.exists():
        shutil.copyfile(example, env_path)
    else:
        env_path.write_text("", encoding="utf-8")


def load_env(env_path: Path) -> dict[str, str]:
    return {key: value or "" for key, value in dotenv_values(env_path).items()}


def ask_text(label: str, current: str = "") -> str:
    prompt = f"{label} [{display_value(label, current)}]: "
    value = input(prompt).strip()
    return current if value == "" else value


def ask_secret(label: str, current: str = "") -> str:
    prompt = f"{label} [{display_value(label, current)}]: "
    while True:
        value = getpass.getpass(prompt).strip()
        if value == "":
            return current
        print(f"Saisie reçue : {secret_preview(value)}")
        if ask_yes_no("Confirmer cette valeur ?", current=True):
            return value


def ask_optional_secret(label: str, current: str = "") -> str:
    return ask_secret(label, current)


def ask_choice(label: str, choices: list[str], current: str = "") -> str:
    normalized_current = current if current in choices else choices[0]
    printable = ", ".join(choice or "empty" for choice in choices)
    while True:
        value = input(f"{label} ({printable}) [{display_value(label, normalized_current)}]: ").strip()
        selected = normalized_current if value == "" else value
        if selected in choices:
            return selected
        print(f"Valeur invalide. Choisis parmi : {printable}")


def ask_daily_time(label: str, current: str = "") -> str:
    while True:
        value = ask_text(label, current)
        try:
            hour, minute = parse_daily_time(value)
        except ValueError as exc:
            print(str(exc))
            current = value
            continue
        return f"{hour:02d}:{minute:02d}"


def ask_yes_no(label: str, current: bool = True) -> bool:
    default = "o" if current else "n"
    while True:
        value = input(f"{label} (o/n) [{default}]: ").strip().lower()
        selected = default if value == "" else value
        if selected in {"o", "oui", "y", "yes"}:
            return True
        if selected in {"n", "non", "no"}:
            return False
        print("Réponds par o/oui ou n/non.")


def parse_daily_time(value: str) -> tuple[int, int]:
    text = value.strip()
    if re.fullmatch(r"\d{1,2}", text):
        hour = int(text)
        minute = 0
    elif re.fullmatch(r"\d{1,2}:\d{2}", text):
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    else:
        raise ValueError("Heure invalide. Utilise HH:MM, par exemple 04:00.")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Heure invalide. Heure 0-23, minute 0-59.")
    return hour, minute


def build_dreaming_cron_line(user_id: str, since_hours: str, daily_time: str, project_root: Path = PROJECT_ROOT) -> str:
    hour, minute = parse_daily_time(daily_time)
    root = shlex.quote(str(project_root))
    user = shlex.quote(user_id)
    hours = shlex.quote(since_hours or DEFAULTS["DREAMING_SINCE_HOURS"])
    return (
        f"{minute} {hour} * * * "
        f"cd {root} && DAEMON_CORE_USER_ID={user} DREAMING_SINCE_HOURS={hours} "
        "./scripts/run_dreaming_daily.sh >> memory/levels/dreaming/cron.log 2>&1"
    )


def update_crontab_content(existing: str, cron_line: str) -> str:
    kept = []
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped == CRON_MARKER:
            continue
        if "scripts/run_dreaming_daily.sh" in line:
            continue
        kept.append(line.rstrip())
    while kept and kept[-1] == "":
        kept.pop()
    if kept:
        kept.append("")
    kept.extend([CRON_MARKER, cron_line])
    return "\n".join(kept).strip() + "\n"


def install_dreaming_cron(cron_line: str) -> None:
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    existing = current.stdout if current.returncode == 0 else ""
    updated = update_crontab_content(existing, cron_line)
    subprocess.run(["crontab", "-"], input=updated, text=True, check=True)


def install_telegram_service(project_root: Path = PROJECT_ROOT) -> None:
    subprocess.run([str(project_root / "scripts" / "install_telegram_service.sh")], check=True)


def import_memory_bundle(bundle_path: Path, project_root: Path = PROJECT_ROOT) -> Path:
    bundle = bundle_path.expanduser()
    if not bundle.exists():
        raise FileNotFoundError(f"Bundle introuvable: {bundle}")
    if bundle.suffix == ".gpg":
        raise ValueError("Bundle chiffré détecté. Déchiffre-le d'abord, puis relance l'import sur le .tar/.tar.gz.")
    memory_root = project_root / "memory"
    backup_path = backup_existing_memory(memory_root)
    with tarfile.open(bundle, mode="r:*") as archive:
        members = archive.getmembers()
        validate_memory_bundle_members(members)
        archive.extractall(project_root, members=members, filter="data")
    return backup_path


def latest_memory_bundle(import_dir: Path) -> Path:
    root = import_dir.expanduser()
    bundles = sorted(root.glob("daemon-core-memory-*.tar.gz"), key=lambda path: path.stat().st_mtime)
    if not bundles:
        raise FileNotFoundError(f"Aucun bundle mémoire trouvé dans {root}")
    return bundles[-1]


def backup_existing_memory(memory_root: Path) -> Path:
    backup_root = memory_root / "levels" / "memory_import_backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_path = backup_root / f"memory_before_import_{timestamp}.tar.gz"
    if memory_root.exists():
        with tarfile.open(backup_path, "w:gz") as archive:
            for child in sorted(memory_root.iterdir()):
                if child == backup_root or backup_root in child.parents:
                    continue
                archive.add(child, arcname=f"memory/{child.name}")
    else:
        with tarfile.open(backup_path, "w:gz"):
            pass
    return backup_path


def validate_memory_bundle_members(members: list[tarfile.TarInfo]) -> None:
    if not members:
        raise ValueError("Bundle mémoire vide.")
    for member in members:
        name = member.name
        normalized = Path(name)
        parts = normalized.parts
        if member.issym() or member.islnk():
            raise ValueError(f"Liens interdits dans le bundle mémoire: {name}")
        if normalized.is_absolute() or ".." in parts:
            raise ValueError(f"Chemin dangereux dans le bundle mémoire: {name}")
        if not parts or parts[0] != "memory":
            raise ValueError(f"Le bundle doit contenir uniquement des chemins sous memory/: {name}")


def display_value(key: str, value: str) -> str:
    if key in SECRET_KEYS:
        return mask_secret(value)
    return value or "empty"


def mask_secret(value: str) -> str:
    if not value:
        return "empty"
    if len(value) <= 8:
        return "********"
    return f"{value[:4]}...{value[-4:]}"


def secret_preview(value: str) -> str:
    if not value:
        return "vide"
    if len(value) <= 8:
        return f"{mask_secret(value)} ({len(value)} caractères)"
    return f"{value[:7]}...{value[-4:]} ({len(value)} caractères)"


def write_env_updates(env_path: Path, updates: dict[str, str]) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    output = []
    pattern = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
    for line in lines:
        match = pattern.match(line)
        if not match:
            output.append(line)
            continue
        key = match.group(1)
        if key in updates:
            output.append(f"{key}={format_env_value(updates[key])}")
            seen.add(key)
        else:
            output.append(line)
    missing = [key for key in ORDERED_KEYS if key in updates and key not in seen]
    if missing and output and output[-1] != "":
        output.append("")
    for key in missing:
        output.append(f"{key}={format_env_value(updates[key])}")
    env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def parse_env_assignment(assignment: str) -> tuple[str, str]:
    if "=" not in assignment:
        raise ValueError("Utilise le format KEY=value.")
    key, value = assignment.split("=", 1)
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ValueError(f"Nom de variable invalide: {key}")
    return key, value


def format_env_value(value: str) -> str:
    if value == "":
        return ""
    if re.fullmatch(r"[A-Za-z0-9_./:@+-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def print_summary(updates: dict[str, str]) -> None:
    print("\nRésumé")
    for key in ORDERED_KEYS:
        if key in updates:
            print(f"- {key}={display_value(key, updates[key])}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure Daemon Harness .env interactively.")
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env"))
    parser.add_argument("--set", dest="env_updates", action="append", default=[], metavar="KEY=value", help="Set one .env variable and exit. Can be repeated.")
    parser.add_argument("--set-secret", metavar="KEY", help="Prompt for one secret .env variable and exit.")
    parser.add_argument("--install-telegram-service", action="store_true", help="Install or update the persistent Telegram systemd service and exit.")
    parser.add_argument("--import-memory-bundle", help="Import a memory .tar/.tar.gz bundle and exit without interactive onboarding.")
    parser.add_argument("--import-latest-memory-bundle", action="store_true", help="Import the newest daemon-core-memory-*.tar.gz from the import directory and exit.")
    parser.add_argument("--memory-import-dir", default="~/daemon-harness-import", help="Directory used by --import-latest-memory-bundle.")
    args = parser.parse_args()
    env_path = Path(args.env_file)
    if args.env_updates and args.set_secret:
        parser.error("--set and --set-secret cannot be used together.")
    if args.import_memory_bundle and args.import_latest_memory_bundle:
        parser.error("--import-memory-bundle and --import-latest-memory-bundle cannot be used together.")
    if args.install_telegram_service:
        install_telegram_service(project_root=PROJECT_ROOT)
        return
    if args.env_updates:
        ensure_env_file(env_path)
        updates = dict(parse_env_assignment(assignment) for assignment in args.env_updates)
        write_env_updates(env_path, updates)
        print_summary(updates)
        return
    if args.set_secret:
        ensure_env_file(env_path)
        key = args.set_secret.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            parser.error(f"Nom de variable invalide: {key}")
        value = ask_secret(key, dotenv_values(env_path).get(key, ""))
        updates = {key: value}
        write_env_updates(env_path, updates)
        print_summary(updates)
        return
    if args.import_latest_memory_bundle:
        bundle_path = latest_memory_bundle(Path(args.memory_import_dir))
        backup_path = import_memory_bundle(bundle_path, project_root=PROJECT_ROOT)
        print(f"Bundle mémoire importé : {bundle_path}")
        print(f"Backup précédent : {backup_path}")
        return
    if args.import_memory_bundle:
        backup_path = import_memory_bundle(Path(args.import_memory_bundle), project_root=PROJECT_ROOT)
        print(f"Bundle mémoire importé. Backup précédent : {backup_path}")
        return
    run_onboarding(env_path=env_path)


if __name__ == "__main__":
    main()
