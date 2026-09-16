"""End-to-end test of the INSTALLED build — the test that was missing.

Everything else in this repo exercises the source tree. This installs
Output\\CaptionBandSetup.exe silently, launches the installed exe
the way an operator would (auto-start), plays real speech through the
speakers, presses the real global hotkey, and reads back app.log.

It temporarily sets auto_start_translation=true in the operator's
config.json (restored afterwards, byte for byte) because there is no other
way to press "Iniciar" on a tray app from a script. Secrets are never read
or printed: they live in the keyring, not in the file.

Run:  python e2e_installed.py            (needs Output\\...Setup.exe built)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time

import keyboard  # type: ignore

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP = os.path.join(HERE, "Output", "CaptionBandSetup.exe")
APPDATA = os.path.join(os.environ["LOCALAPPDATA"], "CaptionBand")
CONFIG = os.path.join(APPDATA, "config.json")
LOG = os.path.join(APPDATA, "app.log")
# The REAL per-user install location (installer.iss: {autopf} with
# PrivilegesRequired=lowest). This test used to install into a temp dir and
# uninstall afterwards — but Inno Setup keys the uninstall registry entry and
# the Start Menu group by AppId, so the temp install's uninstaller removed the
# operator's shortcuts and uninstall entry. The test now IS the install:
# it installs where the operator's copy lives and never uninstalls.
INSTALL_DIR = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "CaptionBand")
EXE = os.path.join(INSTALL_DIR, "CaptionBand.exe")

sys.path.insert(0, HERE)
from config import load_config  # noqa: E402
from e2e_live import play, synthesize  # noqa: E402


def sh(*args) -> int:
    return subprocess.call(list(args))


def main() -> int:
    if not os.path.exists(SETUP):
        print("Setup nao encontrado:", SETUP)
        return 2
    cfg = load_config()
    if not cfg.azure_speech_key:
        print("sem chave Azure no keyring — nada a testar")
        return 2

    wav_pt = synthesize(cfg.azure_speech_key, cfg.azure_speech_region, "pt")
    wav_pt2 = synthesize(cfg.azure_speech_key, cfg.azure_speech_region, "pt2")

    print("[1] instalando em", INSTALL_DIR)
    rc = sh(SETUP, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS")
    print("    setup exit =", rc)
    if rc != 0 or not os.path.exists(EXE):
        return 1

    backup = CONFIG + ".e2e-backup"
    shutil.copy2(CONFIG, backup)
    try:
        raw = json.loads(open(CONFIG, encoding="utf-8").read())
        raw["auto_start_translation"] = True
        raw["provider"] = "azure"
        raw["azure_streaming_mode"] = False
        open(CONFIG, "w", encoding="utf-8").write(json.dumps(raw, indent=2, ensure_ascii=False))

        # A fresh install (or a renamed app) has no log yet: start from zero
        # instead of dying before the test has run anything.
        if os.path.exists(LOG):
            with open(LOG, encoding="utf-8", errors="replace") as fh:
                before = sum(1 for _ in fh)
        else:
            before = 0

        print("[1b] janela de Configuracoes do exe instalado (--settings) fica viva?")
        sw = subprocess.Popen([EXE, "--settings"])
        time.sleep(6.0)
        settings_alive = sw.poll() is None
        print("    settings alive:", settings_alive)
        sw.kill()
        time.sleep(1.0)

        print("[2] abrindo o exe instalado (auto-start)")
        proc = subprocess.Popen([EXE])
        time.sleep(9.0)                      # Qt up + Azure session open

        print("[3] fala em portugues, modo auto-detectar")
        play(wav_pt)
        time.sleep(6.0)

        print("[4] F9 real -> fixar pt-BR")
        keyboard.press_and_release("f9")
        time.sleep(4.0)
        play(wav_pt2)
        time.sleep(6.0)

        print("[5] F9 x3 -> de volta ao auto")
        for _ in range(3):
            keyboard.press_and_release("f9")
            time.sleep(3.5)
        # The last swap runs on a worker (~1.5 s of Azure teardown/setup);
        # killing the process before it logs completion read as a failure
        # once. Give it room.
        time.sleep(6.0)

        alive = proc.poll() is None
        print("    processo vivo ao final:", alive)
        # Kill ONLY the instance this script launched. `taskkill /IM` by
        # image name took the operator's live session down with it once.
        proc.kill()
        time.sleep(1.5)
    finally:
        shutil.copy2(backup, CONFIG)
        os.remove(backup)
        print("    config.json restaurado")

    if not os.path.exists(LOG):
        print("o app nunca escreveu log — nada a verificar")
        return 1
    with open(LOG, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()[before:]
    text = "".join(lines)

    finals = len(re.findall(r"translation event: .*final=True", text))
    partials = len(re.findall(r"translation event: .*final=False", text))
    pinned = len(re.findall(r"provider swapped: streaming=True", text))
    back_auto = len(re.findall(r"provider swapped: streaming=False", text))
    hotkey = "registered global hotkey" in text
    tracebacks = text.count("Traceback")
    errors = [line.strip()[:140] for line in lines if "[ERROR]" in line or "[CRITICAL]" in line]

    print()
    print("legendas finais   :", finals)
    print("parciais (auto)   :", partials, " <- antes desta versao: sempre 0 no modo auto")
    print("fixou idioma (F9) :", pinned, "| voltou ao auto:", back_auto)
    print("hotkey registrada :", hotkey)
    print("tracebacks        :", tracebacks)
    for e in errors[:5]:
        print("  ERROR:", e)

    quit_ok = "app quit normally" in text or True   # killed by us; informative only
    print("settings window   :", "viva" if settings_alive else "MORREU")
    ok = alive and settings_alive and finals >= 2 and partials >= 1 and pinned >= 1 \
        and back_auto >= 1 and hotkey and tracebacks == 0 and quit_ok
    print()
    print("VEREDITO:", "PASSOU — exe instalado legenda, troca idioma por F9 e volta"
          if ok else "FALHOU")

    # No uninstall: this WAS the install. The operator launches it from the
    # Start Menu; the process this script started is already gone.
    print("[6] instalado e pronto em", INSTALL_DIR)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
