# PyInstaller spec for CaptionBand Windows app
# Build with: pyinstaller translator.spec --noconfirm

# pylint: disable=undefined-variable
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None
# SPECPATH is injected by PyInstaller itself and is the directory containing
# this spec — os.getcwd() breaks when pyinstaller is invoked from elsewhere.
project_dir = Path(SPECPATH)

speech_datas, speech_binaries, speech_hidden = collect_all('azure.cognitiveservices.speech')
sd_datas, sd_binaries, sd_hidden = collect_all('sounddevice')
soundcard_datas, soundcard_binaries, soundcard_hidden = collect_all('soundcard')
openai_datas, openai_binaries, openai_hidden = collect_all('openai')
keyring_datas, keyring_binaries, keyring_hidden = collect_all('keyring')

# Google Speech, Whisper local and Argos are NOT bundled — see
# requirements-build.txt for why. Build from .venv-build so they are not even
# installed; the excludes below are the belt to that suspender.

a = Analysis(
    ['translator.py'],
    pathex=[str(project_dir)],
    binaries=[
        *speech_binaries,
        *sd_binaries,
        *soundcard_binaries,
        *openai_binaries,
        *keyring_binaries,
    ],
    datas=[
        *speech_datas,
        *sd_datas,
        *soundcard_datas,
        *openai_datas,
        *keyring_datas,
    ],
    hiddenimports=[
        *speech_hidden,
        *sd_hidden,
        *soundcard_hidden,
        *openai_hidden,
        *keyring_hidden,
        'numpy',
        'PyQt6',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        # Every providers.* submodule, discovered automatically — a new
        # provider no longer needs to be remembered here by hand.
        *collect_submodules('providers'),
        'transcript',
        'secrets_store',
        'constants',
        'updater',
        'ordering',
        'preflight',
        'usage_tracker',
        'connection_test',
        # Third-party imported lazily (inside functions), invisible to the
        # static analysis: the F9 hotkey, the Realtime WebSocket and the
        # 16→24 kHz resampler.
        'keyboard',
        'websocket',
        'keyring.backends.Windows',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'torch',
        'tensorflow',
        'pandas',
        'matplotlib',
        'scipy',
        'sympy',
        'stanza',
        'faster_whisper',
        'ctranslate2',
        'argostranslate',
        'google',
        'grpc',
        'tkinter',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# One-DIR, not one-file. With faster-whisper, ctranslate2 and gRPC bundled
# the payload is hundreds of MB; a one-file exe unpacks all of it to %TEMP%
# on EVERY launch, which is a slow start exactly when the event is about to
# begin. The installer copies the folder once instead.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CaptionBand',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='CaptionBand',
)
